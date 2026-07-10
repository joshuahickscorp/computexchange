#!/usr/bin/env python3
"""
tuner_stack_analytical.py — coordinate-ascent tuner for the ANALYTICAL depth+camera-matrix
reprojection runner (pod/exp_render_stack_analytical.py), the fix built to try to break the
worst_tile_ssim~0.27 ceiling the 2D-motion-vector keystone hit overnight.

First real-hardware check (2026-07-07 morning, minimal frames=2/keyframe_every=2 case):
worst_tile 0.2732 (2D-vector) -> 0.3571 (analytical) — a real, non-trivial improvement
(identity_probe_max_error_px=0.0 on real Cycles data), but still far below any usable
quality tier. This tuner finds the analytical method's REAL ceiling the same way
tuner_stack.py found the 2D-vector method's — full multi-knob coordinate ascent, not a
single hand-picked config.

Same two-constraint feasibility as tuner_stack.py (a good global average can never hide a
collapsed worst tile):

    maximize net_speedup  subject to  quality >= q_floor  AND  worst_tile_ssim >= q_floor-0.05

The runner's OWN identity-probe gate (raises + emits {"error":...} if same-camera
reprojection error exceeds 1e-2px) is the first line of defense against a silently-wrong
projection contaminating a trial; feasible_speedup here additionally REQUIRES a present,
finite identity_probe_max_error_px below a sanity threshold before trusting quality/worst_tile
at all — belt and suspenders, since a passing run should always have a tiny probe error.

Same reference-cache reuse (scene, resolution, ref_spp, bounces, frames, seed, cam_motion) as
exp_render_stack.py — resolution is the only cache-invalidating swept knob, kept LAST.

Money-safety mirrors tuner_stack.py: does NOT provision its own pod — the caller
(run_stack_analytical_tuner.py) provisions ONE reachable pod, ships pod/, installs EXR deps,
and passes the pod object in.
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
LEDGER = os.path.join(LEDGER_DIR, "stack_analytical_tuner_ledger.jsonl")

TARGET_ID = "render_stack_analytical"
RUNNER = "exp_render_stack_analytical.py"
TRIAL_TIMEOUT_S = 3600

# Same tiers/ladder as tuner_stack.py — directly comparable Pareto frontiers.
Q_TIERS = [0.99, 0.98, 0.95, 0.90]
TILE_FLOOR_DELTA = 0.05

# Sanity ceiling on the identity probe (px). The runner itself already raises/errors above
# 1e-2px; this is belt-and-suspenders so a borderline-but-passing probe still can't be
# trusted for a tier decision if something looks off.
IDENTITY_PROBE_SANITY_PX = 1e-2


def tile_floor(q_floor):
    return q_floor - TILE_FLOOR_DELTA


# --------------------------------------------------------------------------- #
# BASE CONFIG — mirrors tuner_stack.py's tight start, plus the analytical-only
# knobs pinned to their safe/self-calibrating defaults (not swept: 'auto' depth
# convention and the identity-probe safety gate stay ON in every trial).
# --------------------------------------------------------------------------- #
BASE = {
    "frames": 8,
    "keyframe_every": 2,
    "draft_spp": 512,
    "ref_spp": 4096,
    "adaptive_threshold": 0.02,
    "denoiser": "oidn",
    "denoise_guides": True,
    "light_tree": True,
    "hole_fill": "rerender",
    "disocclusion_thresh": 0.1,   # now a RELATIVE-DEPTH tolerance, not an MV-divergence one
    "resolution": "1920x1080",
    "scene": "classroom",
    "bounces": 12,
    "device": "AUTO",
    "depth_convention": "auto",   # self-calibrating; not swept
    "probe_identity": True,       # safety gate always on
}

# --------------------------------------------------------------------------- #
# SEARCH SPACE — same ordering discipline as tuner_stack.py: resolution LAST
# (the only cache-invalidating knob). disocclusion_thresh is NEW here (its
# semantics changed from MV-divergence to relative-depth tolerance) — swept
# early alongside keyframe_every since it is the other primary disocclusion lever.
# --------------------------------------------------------------------------- #
SPACE = {
    "keyframe_every":      [2, 3, 4, 6, 8],
    "disocclusion_thresh": [0.02, 0.05, 0.1, 0.2, 0.3],
    "draft_spp":           [256, 384, 512, 768, 1024],
    "adaptive_threshold":  [0.005, 0.01, 0.02, 0.03, 0.05],
    "hole_fill":           ["rerender", "inpaint", "nearest"],
    "light_tree":          [True, False],
    "denoiser":            ["oidn", "optix"],
    "resolution":          ["1280x720", "1920x1080"],
}

CACHE_INVALIDATING_KNOBS = {"resolution"}
PRIMARY_KNOB = "keyframe_every"


def _now():
    return datetime.now(timezone.utc).isoformat()


def log(m):
    print(f"[tuner_stack_analytical {time.strftime('%H:%M:%S')}] {m}", flush=True)


def ledger_append(rec):
    os.makedirs(LEDGER_DIR, exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps({"ts": _now(), **rec}) + "\n")


def cfg_key(cfg):
    return TARGET_ID + "|" + json.dumps(cfg, sort_keys=True)


def load_cache():
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


def run_trial(pod, cfg, cache):
    key = cfg_key(cfg)
    if key in cache:
        return cache[key]
    payload = json.dumps(cfg).replace("'", "'\\''")
    cmd = f"cd /root/spec-lab && python3 pod/{RUNNER} '{payload}'"
    try:
        rc, out, err = runpod.ssh(pod, cmd, timeout=TRIAL_TIMEOUT_S)
    except Exception as e:
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
    probe = m.get("identity_probe_max_error_px")
    log(f"    {cfg} -> speedup={sp} q={q} worst_tile={wt} probe_px={probe}"
        + ("  ERR:" + str(m.get("error"))[:120] if "error" in m else ""))
    return m


def feasible_speedup(m, qfloor):
    if not isinstance(m, dict) or "error" in m:
        return float("-inf")
    q = m.get("quality")
    wt = m.get("worst_tile_ssim")
    sp = m.get("net_speedup")
    probe = m.get("identity_probe_max_error_px")
    if q is None or wt is None or sp is None:
        return float("-inf")
    # Belt-and-suspenders: don't trust a trial's quality/worst_tile unless the identity
    # probe ran and passed sanity (the runner should already have errored above 1e-2px,
    # this just refuses ambiguous/missing-probe trials at the tuner level too).
    if probe is None or not (probe == probe) or probe > IDENTITY_PROBE_SANITY_PX:
        return float("-inf")
    if q >= qfloor and wt >= tile_floor(qfloor):
        return sp
    return float("-inf")


def ofat(pod, cache, budget_ok):
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
    cur = dict(start_cfg)
    cur_s = feasible_speedup(run_trial(pod, cur, cache), qfloor)
    improved = True
    while improved and budget_ok():
        improved = False
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


def tune_stack_analytical(pod, max_minutes, min_balance_check_fn):
    deadline = time.time() + max_minutes * 60

    def budget_ok():
        return time.time() < deadline and bool(min_balance_check_fn())

    cache = load_cache()
    log(f"analytical stack tuner: {len(cache)} cached trials loaded; max {max_minutes}m; "
        f"tiers {Q_TIERS}; base keyframe_every={BASE['keyframe_every']} (TIGHT start)")

    log("=== OFAT sweep (one knob at a time off the base) ===")
    trials = ofat(pod, cache, budget_ok)

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

    rounds = 0
    while budget_ok():
        rounds += 1
        progressed = False
        qf = Q_TIERS[0]
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
            log(f"refine round {rounds}: no improvement — analytical stack tuning converged.")
            break

    front = pareto_front(cache)
    ledger_append({"event": "pareto", "target": TARGET_ID,
                   "front": [{"quality": q, "speedup": s, "worst_tile_ssim": wt}
                             for q, s, wt, _ in front]})
    log("analytical stack tuning loop complete.")
    return {
        "tier_best": {str(qf): tier_best.get(qf) for qf in Q_TIERS},
        "pareto": [{"quality": q, "speedup": s, "worst_tile_ssim": wt}
                   for q, s, wt, _ in front],
    }
