#!/usr/bin/env python3
"""run_prod_render.py — one money-safe big-GPU session for the DENOISE-ANCHOR experiment.

Provisions a production GPU (A100 preferred), ships the pod runners, renders the Classroom
reference ONCE (cached on the pod), then sweeps draft operating points reusing that
reference — collecting HONEST net_speedup + global/worst-tile SSIM from
pod/exp_cycles_render_prod.py. Tears down on EVERY exit path (atexit + SIGTERM via
runpod.register_cleanup). This is the decisive test of whether adaptive-sampling +
OIDN(albedo/normal) denoise reaches >=0.97 SSIM at >=8x in the PRODUCTION regime our
small-scene tests never reached.

Usage: RUNPOD_API_KEY=... python3 run_prod_render.py [--max-minutes 45] [--min-balance 6]
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
POD_DISK_GB = 80  # classroom.zip + Blender + reference/draft PNGs

RUNNER = "exp_cycles_render_prod.py"
LEDGER = os.path.join(REPO, "docs/speed-lane-reports/spec-lab/prod_render_ledger.jsonl")

# --- the anchor experiment matrix -------------------------------------------
# The reference (classroom 1080p 4096spp) renders once on the first call and is CACHED
# on the pod by (scene,res,ref_spp,bounces); every later draft reuses it for free.
BASE = {"scene": "classroom", "resolution": "1920x1080", "ref_spp": 4096,
        "bounces": 12, "device": "AUTO"}
DRAFTS = [
    # the contender: adaptive sampling + OIDN + albedo/normal guides
    {"name": "contender-thr0.01-spp512", "draft_spp": 512, "adaptive": True,
     "adaptive_threshold": 0.01, "denoiser": "oidn", "denoise_guides": True},
    # looser + tighter adaptive thresholds -> trace the speed<->quality frontier
    {"name": "thr0.02-spp512", "draft_spp": 512, "adaptive": True,
     "adaptive_threshold": 0.02, "denoiser": "oidn", "denoise_guides": True},
    {"name": "thr0.005-spp1024", "draft_spp": 1024, "adaptive": True,
     "adaptive_threshold": 0.005, "denoiser": "oidn", "denoise_guides": True},
    # CONTROL: guides OFF (proves albedo/normal guidance is the quality lever)
    {"name": "noguides-thr0.01", "draft_spp": 512, "adaptive": True,
     "adaptive_threshold": 0.01, "denoiser": "oidn", "denoise_guides": False},
    # denoiser A/B: OptiX instead of OIDN
    {"name": "optix-thr0.01", "draft_spp": 512, "adaptive": True,
     "adaptive_threshold": 0.01, "denoiser": "optix", "denoise_guides": True},
    # naive low-spp baseline (the config that FAILED on small scenes) in the prod regime
    {"name": "naive-spp32-fixed", "draft_spp": 32, "adaptive": False,
     "adaptive_threshold": 0.01, "denoiser": "oidn", "denoise_guides": True},
]


def log(m):
    print(f"[prod-render {time.strftime('%H:%M:%S')}] {m}", flush=True)


def ledger_append(rec):
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}) + "\n")


def run_runner(pod, cfg, timeout):
    """SSH the runner with a config JSON; return the parsed final-line JSON (or {'error'})."""
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
    ap.add_argument("--max-minutes", type=int, default=45)
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
        log("runners shipped. first call bootstraps apt libs + Blender + scene + reference "
            "render (slow); later calls reuse the cached reference.")

        first = True
        for d in DRAFTS:
            if time.time() > deadline:
                log("deadline reached — stopping the sweep.")
                break
            bal = runpod.balance()["clientBalance"]
            if bal <= args.min_balance:
                log(f"balance ${bal:.2f} at/below floor ${args.min_balance} — stopping.")
                break
            cfg = {**BASE, **{k: v for k, v in d.items() if k != "name"}}
            # generous timeout on the first call (Blender + scene download + reference render)
            timeout = 1500 if first else 600
            log(f"RUN {d['name']} (timeout {timeout}s)…")
            t0 = time.time()
            res = run_runner(pod, cfg, timeout)
            dt = time.time() - t0
            res_row = {"event": "trial", "name": d["name"], "config": cfg,
                       "wall_s_incl_ssh": round(dt, 1), "result": res}
            ledger_append(res_row)
            if res.get("error"):
                log(f"  {d['name']} -> ERROR: {str(res['error'])[:180]}")
            else:
                log(f"  {d['name']} -> net_speedup={res.get('net_speedup')}x "
                    f"quality={res.get('quality')} worst_tile={res.get('worst_tile_ssim')} "
                    f"p5_tile={res.get('p5_tile_ssim')} "
                    f"(ref={res.get('ref_render_s')}s draft={res.get('draft_render_s')}s "
                    f"cache_hit={res.get('ref_cache_hit')} dev={res.get('device')})")
            first = False
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
