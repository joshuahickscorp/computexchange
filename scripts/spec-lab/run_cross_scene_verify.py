#!/usr/bin/env python3
"""run_cross_scene_verify.py — TRACK 1: does the denoise-anchor result generalize?

The anchor was proven on Classroom (matte/diffuse-heavy) at 1080p: ~5.3x@0.978 near-lossless,
~9.5x@0.964 preview. Two open questions before trusting it as a general product claim:
  1. Does it hold on a SPECULAR/GLOSSY scene (BMW27 — car paint + environment reflections,
     the classic denoiser failure mode: over-smoothed highlights)?
  2. Does the win GROW at 4K as predicted (fixed overhead a smaller fraction of a bigger
     render)?

Each (scene, resolution) combo needs its OWN reference render (the ref-cache key includes
scene+resolution, so nothing here reuses tonight's Classroom/1080p cache) — cost is paid
once per combo, then the draft configs for that combo reuse it. Uses the proven
exp_cycles_render_prod.py runner unmodified. Tears down on every exit path.
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import runpod  # noqa: E402

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
POD_IMAGE = "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"
POD_DISK_GB = 100  # bigger: BMW27 + a 4K classroom reference/draft PNG pair

RUNNER = "exp_cycles_render_prod.py"
LEDGER = os.path.join(REPO, "docs/speed-lane-reports/spec-lab/cross_scene_ledger.jsonl")

# CONTENDER = tonight's winning near-lossless operating point.
CONTENDER = {"draft_spp": 512, "adaptive": True, "adaptive_threshold": 0.02,
             "denoiser": "oidn", "denoise_guides": True}
NAIVE = {"draft_spp": 32, "adaptive": False, "adaptive_threshold": 0.02,
         "denoiser": "oidn", "denoise_guides": True}
NOGUIDES = {"draft_spp": 512, "adaptive": True, "adaptive_threshold": 0.02,
            "denoiser": "oidn", "denoise_guides": False}

# --- the cross-scene / cross-resolution matrix ------------------------------
COMBOS = [
    # BMW27: specular/glossy stress test — the classic denoiser failure mode
    # (over-smoothed highlights). Does the anchor still clear near-lossless?
    {"scene": "bmw27", "resolution": "1920x1080", "ref_spp": 4096, "bounces": 12,
     "device": "AUTO",
     "drafts": [("bmw27-contender", CONTENDER), ("bmw27-naive", NAIVE),
                ("bmw27-noguides", NOGUIDES)]},
    # Classroom @ 4K: does the win GROW as the design panel predicted (fixed
    # overhead a smaller fraction of a bigger, slower render)?
    {"scene": "classroom", "resolution": "3840x2160", "ref_spp": 4096, "bounces": 12,
     "device": "AUTO",
     "drafts": [("classroom-4k-contender", CONTENDER),
                ("classroom-4k-naive", NAIVE)]},
]


def log(m):
    print(f"[cross-scene {time.strftime('%H:%M:%S')}] {m}", flush=True)


def ledger_append(rec):
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}) + "\n")


def run_runner(pod, cfg, timeout):
    payload = json.dumps(cfg).replace("'", "'\\''")
    cmd = f"cd /root/spec-lab && python3 pod/{RUNNER} '{payload}'"
    rc, out, err = runpod.ssh(pod, cmd, timeout=timeout)
    tail = [ln for ln in (out or "").splitlines() if ln.strip()]
    if not tail:
        return {"error": f"no stdout (rc={rc}); stderr_tail={(err or '')[-300:]}"}
    try:
        return json.loads(tail[-1])
    except Exception as e:
        return {"error": f"unparseable final line: {e}; last={tail[-1][:200]}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-minutes", type=int, default=90)
    ap.add_argument("--min-balance", type=float, default=6.0)
    args = ap.parse_args()

    runpod.register_cleanup()
    deadline = time.time() + args.max_minutes * 60
    bal0 = runpod.balance()["clientBalance"]
    log(f"balance ${bal0:.2f}; floor ${args.min_balance}; max {args.max_minutes}m")
    if bal0 <= args.min_balance:
        log("balance already at/below floor — aborting before provisioning.")
        return

    log("provisioning a production GPU (A100 preferred)…")
    pod = runpod.provision_reachable(GPU_PLAN, POD_IMAGE, disk_gb=POD_DISK_GB)
    ledger_append({"event": "pod_up", "pod": pod})
    log(f"pod {pod['gpu']} {pod['id']} @ {pod['ip']}:{pod['port']}")

    try:
        rc, _, err = runpod.ssh(pod, "mkdir -p /root/spec-lab", timeout=60)
        if rc != 0:
            raise RuntimeError(f"mkdir failed: {err[:200]}")
        ok, serr = runpod.scp_to(pod, os.path.join(HERE, "pod"), "/root/spec-lab/")
        if not ok:
            raise RuntimeError(f"scp pod/ failed: {serr[:200]}")
        log("runners shipped.")

        for combo in COMBOS:
            scene, res = combo["scene"], combo["resolution"]
            base = {k: v for k, v in combo.items() if k != "drafts"}
            first_in_combo = True
            for name, draft in combo["drafts"]:
                if time.time() > deadline:
                    log("deadline reached — stopping.")
                    break
                bal = runpod.balance()["clientBalance"]
                if bal <= args.min_balance:
                    log(f"balance ${bal:.2f} at/below floor ${args.min_balance} — stopping.")
                    break
                cfg = {**base, **draft}
                # generous timeout on the first call per combo (fresh scene download +
                # a NEW reference render — 4K is ~4x the pixels of 1080p).
                timeout = 2400 if first_in_combo else 900
                log(f"RUN {name} [{scene}@{res}] (timeout {timeout}s)…")
                t0 = time.time()
                res_json = run_runner(pod, cfg, timeout)
                dt = time.time() - t0
                ledger_append({"event": "trial", "name": name, "config": cfg,
                               "wall_s_incl_ssh": round(dt, 1), "result": res_json})
                if res_json.get("error"):
                    log(f"  {name} -> ERROR: {str(res_json['error'])[:200]}")
                else:
                    log(f"  {name} -> net_speedup={res_json.get('net_speedup')}x "
                        f"quality={res_json.get('quality')} "
                        f"worst_tile={res_json.get('worst_tile_ssim')} "
                        f"p5_tile={res_json.get('p5_tile_ssim')} "
                        f"(ref={res_json.get('ref_render_s')}s "
                        f"draft={res_json.get('draft_render_s')}s "
                        f"cache_hit={res_json.get('ref_cache_hit')})")
                first_in_combo = False
    finally:
        log("tearing down pod…")
        try:
            runpod.terminate(pod["id"])
        except Exception as e:
            log(f"terminate error (verify in console): {e}")
        b2 = runpod.balance()["clientBalance"]
        ledger_append({"event": "pod_down", "balance_after": b2})
        log(f"pod down. balance ${b2:.2f} (spent ${bal0 - b2:.2f})")


if __name__ == "__main__":
    main()
