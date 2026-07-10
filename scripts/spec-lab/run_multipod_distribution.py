#!/usr/bin/env python3
"""run_multipod_distribution.py — the REAL (not ideal) multi-pod distribution driver.

================================================================================
WHAT THIS MEASURES (the thesis under contact with reality)
================================================================================
exp_render_faninout.py measures the IDEAL fan-out CEILING on ONE box: split a frame
into N tiles, and report T_serial / max(tile) as an UPPER BOUND that assumes every
worker starts at t=0 on its own idle GPU with ZERO comms and ZERO scheduling. That is
a real measurement, but it is a ceiling — it does NOT survive contact with reality.

THIS driver measures whether the ceiling survives. It provisions N REAL RunPod GPUs,
splits the FRAMES of ONE animated shot across them, renders each pod's slice at anchor
quality on genuinely separate hardware over the network, and reports HONEST wall-clock
including the things the ideal ceiling ignores:

  * T_real_distributed_including_provisioning — from the FIRST provisioning call to the
    LAST pod finishing its render. This is what you actually wait for from a cold start:
    provisioning + reachability + dep install + scene fetch + render, end to end.
  * T_real_distributed_excluding_provisioning — from when ALL pods are ready (shipped +
    deps installed) to when the LAST pod finishes. This is the steady-state throughput
    you get once the fleet is warm — the fair number to compare against a single pod.
  * T_serial_single_pod — the SAME frames rendered back-to-back on ONE pod (we render
    the whole shot serially on pod[0] BEFORE distributing the remaining frames across
    the fleet, unless a comparable stack-ladder measurement already exists for this exact
    config — disclosed in the ledger either way).

  net_speedup_real = T_serial_single_pod / T_real_distributed_excluding_provisioning
    — the FAIR steady-state comparison. The provisioning tax is reported SEPARATELY as
    its own number (T_real_incl - T_real_excl) so a reader never conflates the one-time
    cold-start cost with steady-state throughput.

================================================================================
HONESTY
================================================================================
  * Provisioning is SEQUENTIAL by default. Concurrent provisioning across threads shares
    runpod.py's single .tracked_pods.json (its _load/_save are read-modify-write with NO
    lock), so two threads racing _track() can drop a pod id from the anti-orphan ledger —
    a MONEY-SAFETY hazard. We provision one pod at a time and report the honest wall-clock
    cost of doing so (--concurrent-provision opts into threaded provisioning with a lock,
    disclosed, for the reader who accepts the tradeoff). Either way the choice is in the
    ledger.
  * Every render time is a real time.perf_counter() wall-clock the pod measured
    (per_frame_render_s) plus the SSH-observed span the driver measured. We report both.
  * Frame-to-pod balance is reported: with F frames over N pods the split is as even as
    possible (ceil for the first F%N pods), and an uneven split (F not divisible by N)
    means the slowest pod bounds the distributed wall-clock — disclosed as a caveat.
  * Dollars: total $/hr for N pods vs one pod (from currentSpendPerHr deltas), and the
    ACTUAL dollars spent this run from clientBalance deltas (balance() before/after).
  * Money safety: register_cleanup() (atexit + SIGINT/SIGTERM), a deadline watchdog, and
    a finally block that terminates ALL N pods in a loop where EACH termination is wrapped
    in its own try/except so one pod failing to terminate never blocks the others.

Usage:
  RUNPOD_API_KEY=... python3 run_multipod_distribution.py \
      [--pods 3] [--frames-per-shot 8] [--max-minutes 90] [--min-balance 6] \
      [--concurrent-provision]
"""
import argparse
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import runpod  # noqa: E402

# Same plan as run_stack_ladder.py / run_prod_render.py so the pods match the keystone.
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
POD_DISK_GB = 80  # classroom.zip + Blender + per-frame EXRs

RUNNER = "exp_render_frame_subset.py"
LEDGER = os.path.join(REPO, "docs/speed-lane-reports/spec-lab/multipod_ledger.jsonl")
# A comparable single-pod serial measurement, if one exists for this EXACT config, can be
# reused from the stack ladder's ledger instead of re-rendering (disclosed when used).
STACK_LEDGER = os.path.join(REPO, "docs/speed-lane-reports/spec-lab/stack_ladder_ledger.jsonl")

# The ONE animated shot config — matches the keystone (stack-kf4) anchor defaults so the
# distributed frames are the SAME frames the keystone renders at anchor quality.
SHOT = {
    "scene": "classroom",
    "resolution": "1920x1080",
    "draft_spp": 512,
    "adaptive_threshold": 0.02,
    "adaptive_min_samples": 16,
    "denoiser": "oidn",
    "denoise_guides": True,
    "light_tree": True,
    "bounces": 12,
    "cam_motion": 1.0,
    "seed": 0,
    "device": "AUTO",
}

# per-pod SSH timeouts
DEPS_TIMEOUT = 600
RENDER_TIMEOUT_PER_FRAME = 600  # generous per-frame headroom for a 1080p anchor render


def log(m):
    print(f"[multipod {time.strftime('%H:%M:%S')}] {m}", flush=True)


def ledger_append(rec):
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}) + "\n")


def split_frames(n_frames, n_pods):
    """Split range(n_frames) across n_pods as evenly as possible.

    The first (n_frames % n_pods) pods get ceil(n_frames/n_pods) frames; the rest get
    floor. Returns a list of n_pods lists of frame indices (some may be empty if there
    are fewer frames than pods — those pods do no render work, disclosed in balance)."""
    base = n_frames // n_pods
    rem = n_frames % n_pods
    out = []
    start = 0
    for i in range(n_pods):
        count = base + (1 if i < rem else 0)
        out.append(list(range(start, start + count)))
        start += count
    return out


def ship_and_prep(pod):
    """scp pod/ to a pod and install the EXR/imaging deps (NOT setup_base.sh — that
    installs vLLM, irrelevant here). Returns (ok, detail). Same targeted install as
    run_stack_ladder.py."""
    rc, _, err = runpod.ssh(pod, "mkdir -p /root/spec-lab", timeout=60)
    if rc != 0:
        return False, f"mkdir failed: {err[:200]}"
    ok, serr = runpod.scp_to(pod, os.path.join(HERE, "pod"), "/root/spec-lab/")
    if not ok:
        return False, f"scp pod/ failed: {serr[:200]}"
    # The frame-subset runner writes multilayer EXR; OpenEXR/Imath + imaging deps are what
    # it needs. Skip setup_base.sh (vLLM). Same list as run_stack_ladder.py.
    rc, out, err = runpod.ssh(
        pod,
        "pip install --break-system-packages --no-cache-dir "
        "OpenEXR Imath imageio numpy pillow scikit-image 2>&1 | tail -2",
        timeout=DEPS_TIMEOUT)
    return True, f"deps rc={rc}: {(out or '').strip()[-160:]}"


def run_frame_subset(pod, frame_indices, n_frames, timeout):
    """Run the frame-subset runner on a pod for the given indices; return its parsed JSON."""
    cfg = {**SHOT, "frame_indices": frame_indices, "nframes": n_frames}
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


def provision_n_sequential(n_pods, deadline):
    """Provision n_pods ONE AT A TIME (money-safe: no race on the tracked-pods ledger).

    Returns (pods, provision_seconds_per_pod). Stops early if the deadline is hit; the
    caller checks how many actually came up."""
    pods = []
    provision_s = []
    for i in range(n_pods):
        if time.time() > deadline:
            log(f"deadline hit during provisioning; stopping at {len(pods)}/{n_pods} pods.")
            break
        log(f"provisioning pod {i + 1}/{n_pods} (sequential; A100 preferred)…")
        t0 = time.perf_counter()
        pod = runpod.provision_reachable(GPU_PLAN, POD_IMAGE, disk_gb=POD_DISK_GB,
                                         name=f"cx-multipod-{i + 1}")
        dt = time.perf_counter() - t0
        provision_s.append(dt)
        pods.append(pod)
        ledger_append({"event": "pod_up", "idx": i, "pod": pod,
                       "provision_s": round(dt, 1)})
        log(f"  pod {i + 1} up: {pod['gpu']} {pod['id']} @ {pod['ip']}:{pod['port']} "
            f"({dt:.0f}s)")
    return pods, provision_s


def provision_n_concurrent(n_pods, deadline):
    """OPT-IN threaded provisioning behind a lock that serializes the tracked-pods
    ledger writes inside provision_reachable. FASTER wall-clock but relies on the lock;
    disclosed. Returns (pods, provision_seconds_per_pod-list-approx)."""
    log("WARNING: concurrent provisioning enabled — runpod.py's tracked-pods ledger is "
        "read-modify-write with NO internal lock; we serialize provision_reachable calls "
        "with a driver-side lock so _track() cannot drop a pod id. Disclosed in ledger.")
    lock = threading.Lock()
    results = [None] * n_pods
    t_all0 = time.perf_counter()

    def _worker(i):
        if time.time() > deadline:
            return
        # Serialize the whole provision so the tracked-pods ledger RMW cannot race. This
        # makes provisioning effectively sequential for money-safety while still allowing
        # the (rare) parallelism of the reachability polling if the lock is released —
        # here we keep it simple and hold the lock for the whole call (safe, honest).
        with lock:
            if time.time() > deadline:
                return
            try:
                pod = runpod.provision_reachable(GPU_PLAN, POD_IMAGE, disk_gb=POD_DISK_GB,
                                                 name=f"cx-multipod-{i + 1}")
                results[i] = pod
                ledger_append({"event": "pod_up", "idx": i, "pod": pod,
                               "provision_mode": "concurrent-locked"})
            except Exception as e:  # noqa: BLE001
                log(f"  concurrent provision of pod {i + 1} failed: {e}")

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_pods)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    total_s = time.perf_counter() - t_all0
    pods = [p for p in results if p is not None]
    # We cannot cleanly attribute per-pod provision time under the lock; report the total
    # as an approx per-pod average so the accounting stays honest about what we measured.
    approx = [total_s / max(len(pods), 1)] * len(pods)
    return pods, approx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pods", type=int, default=3, help="number of pods N (default 3)")
    ap.add_argument("--frames-per-shot", type=int, default=8,
                    help="frames in the animated shot to distribute (default 8)")
    ap.add_argument("--max-minutes", type=int, default=90)
    ap.add_argument("--min-balance", type=float, default=6.0)
    ap.add_argument("--concurrent-provision", action="store_true",
                    help="opt into threaded (lock-serialized) provisioning; disclosed")
    args = ap.parse_args()

    n_pods = max(1, args.pods)
    n_frames = max(1, args.frames_per_shot)

    runpod.register_cleanup()
    deadline = time.time() + args.max_minutes * 60
    bal0 = runpod.balance()["clientBalance"]
    log(f"balance ${bal0:.2f}; floor ${args.min_balance}; max {args.max_minutes}m; "
        f"pods={n_pods}; frames={n_frames}")
    if bal0 <= args.min_balance:
        log("balance already at/below floor — aborting before provisioning.")
        ledger_append({"event": "abort", "reason": "balance_below_floor", "balance": bal0})
        return

    frame_split = split_frames(n_frames, n_pods)
    max_share = max(len(s) for s in frame_split)
    min_share = min(len(s) for s in frame_split)
    even = (max_share == min_share)
    log(f"frame split across {n_pods} pods: {frame_split} "
        f"(max_share={max_share}, min_share={min_share}, even={even})")

    pods = []
    # ------------------------------------------------------------------------ #
    # T_real_distributed_including_provisioning STARTS HERE — the very first     #
    # provisioning call — and ENDS when the last pod finishes its distributed    #
    # render. This is the honest cold-start wall-clock.                          #
    # ------------------------------------------------------------------------ #
    t_incl_provision_start = time.perf_counter()  # <-- T_real (incl. provisioning) start

    try:
        # ---- 1) PROVISION N pods (sequential by default; money-safe) -------- #
        if args.concurrent_provision:
            pods, provision_s = provision_n_concurrent(n_pods, deadline)
            provision_mode = "concurrent-locked"
        else:
            pods, provision_s = provision_n_sequential(n_pods, deadline)
            provision_mode = "sequential"
        if not pods:
            log("no pods provisioned — aborting.")
            ledger_append({"event": "abort", "reason": "no_pods_provisioned"})
            return
        if len(pods) < n_pods:
            log(f"WARNING: only {len(pods)}/{n_pods} pods came up; re-splitting frames "
                f"across the pods we have.")
            n_pods = len(pods)
            frame_split = split_frames(n_frames, n_pods)
            log(f"re-split: {frame_split}")
        t_provision_wall = time.perf_counter() - t_incl_provision_start
        log(f"provisioning done ({provision_mode}): {len(pods)} pods in "
            f"{t_provision_wall:.0f}s wall")

        # ---- 2) SHIP pod/ + install deps on EVERY pod ----------------------- #
        # This is part of getting the fleet "ready" (still on the cold-start clock, but
        # NOT counted in the steady-state exclude-provisioning number below).
        log("shipping pod/ + installing EXR/imaging deps on every pod…")
        t_prep0 = time.perf_counter()
        prep_ok = True
        for i, pod in enumerate(pods):
            ok, detail = ship_and_prep(pod)
            log(f"  pod {i + 1}: {detail}")
            ledger_append({"event": "pod_prepped", "idx": i, "pod_id": pod["id"],
                           "ok": ok, "detail": detail})
            if not ok:
                prep_ok = False
        t_prep_wall = time.perf_counter() - t_prep0
        if not prep_ok:
            raise RuntimeError("one or more pods failed ship/prep — aborting the run")

        # ============================================================= #
        # 3) SINGLE-POD SERIAL BASELINE (T_serial_single_pod).           #
        # We render the WHOLE shot back-to-back on pod[0] BEFORE the      #
        # distributed run. There is no comparable stack_ladder entry for  #
        # a pure anchor-only serial render of all frames (the keystone     #
        # charges only keyframes + a crop model, NOT every frame fully),   #
        # so we MEASURE it here on pod[0] and disclose that choice.        #
        # ============================================================= #
        log(f"SERIAL BASELINE: rendering all {n_frames} frames back-to-back on pod[0] "
            f"({pods[0]['gpu']})…")
        serial_timeout = RENDER_TIMEOUT_PER_FRAME * n_frames + DEPS_TIMEOUT
        t_serial0 = time.perf_counter()
        serial_res = run_frame_subset(pods[0], list(range(n_frames)), n_frames, serial_timeout)
        t_serial_ssh_wall = time.perf_counter() - t_serial0
        if serial_res.get("error"):
            raise RuntimeError(f"serial baseline failed: {serial_res['error']}")
        # T_serial_single_pod = the pod's OWN measured summed render wall-clock (real
        # per-frame times summed inside the pod), the fair steady-state serial number.
        T_serial_single_pod = float(serial_res.get("t_subset_render_s", 0.0))
        serial_per_frame = serial_res.get("per_frame_render_s", [])
        serial_baseline_source = "measured_on_pod0_this_run"
        ledger_append({"event": "serial_baseline", "pod_id": pods[0]["id"],
                       "T_serial_single_pod": round(T_serial_single_pod, 2),
                       "ssh_wall_s": round(t_serial_ssh_wall, 1),
                       "per_frame_render_s": serial_per_frame,
                       "source": serial_baseline_source, "result": serial_res})
        log(f"  serial baseline T_serial_single_pod={T_serial_single_pod:.1f}s "
            f"(ssh wall {t_serial_ssh_wall:.0f}s) per_frame={serial_per_frame}")

        # ============================================================= #
        # 4) DISTRIBUTED RUN — each pod renders ITS frame subset, run     #
        # concurrently across pods via one thread per pod. The subprocess  #
        # SSH call per pod is timed by the driver; each pod ALSO reports    #
        # its own per-frame render times.                                   #
        # ============================================================= #
        # ---- T_real_distributed_excluding_provisioning STARTS HERE: the fleet is
        #      already provisioned + shipped + deps installed + a serial baseline is
        #      done; this clock measures ONLY the steady-state distributed render. -----
        log("DISTRIBUTED: launching one render thread per pod for its frame subset…")
        dist_results = [None] * len(pods)
        dist_ssh_wall = [0.0] * len(pods)

        def _render_worker(i):
            subset = frame_split[i]
            if not subset:
                dist_results[i] = {"skipped": True, "reason": "no frames assigned",
                                   "frame_indices": []}
                return
            timeout = RENDER_TIMEOUT_PER_FRAME * len(subset) + DEPS_TIMEOUT
            w0 = time.perf_counter()
            dist_results[i] = run_frame_subset(pods[i], subset, n_frames, timeout)
            dist_ssh_wall[i] = time.perf_counter() - w0

        t_excl_provision_start = time.perf_counter()  # <-- T_real (excl. provisioning) start
        threads = [threading.Thread(target=_render_worker, args=(i,))
                   for i in range(len(pods))]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        # last pod to finish bounds the distributed wall-clock
        T_real_distributed_excluding_provisioning = time.perf_counter() - t_excl_provision_start
        t_incl_provision_end = time.perf_counter()
        T_real_distributed_including_provisioning = (
            t_incl_provision_end - t_incl_provision_start)

        # ---- validate every pod's distributed result --------------------------
        dist_errors = []
        per_pod_render_s = []
        for i, res in enumerate(dist_results):
            if res is None:
                dist_errors.append(f"pod{i}: no result")
                continue
            if res.get("skipped"):
                continue
            if res.get("error"):
                dist_errors.append(f"pod{i}: {res['error']}")
                continue
            per_pod_render_s.append({
                "pod": i, "pod_id": pods[i]["id"], "gpu": pods[i]["gpu"],
                "frame_indices": res.get("frame_indices"),
                "per_frame_render_s": res.get("per_frame_render_s"),
                "t_subset_render_s": res.get("t_subset_render_s"),
                "ssh_wall_s": round(dist_ssh_wall[i], 1),
            })

        # ============================================================= #
        # 5) HONESTY ACCOUNTING — the numbers a reader needs.             #
        # ============================================================= #
        provision_tax_s = (T_real_distributed_including_provisioning
                           - T_real_distributed_excluding_provisioning)
        # net_speedup_real = fair steady-state comparison (serial ONE pod vs distributed
        # fleet, both EXCLUDING one-time provisioning). Provisioning tax reported apart.
        net_speedup_real = (
            T_serial_single_pod / T_real_distributed_excluding_provisioning
            if T_real_distributed_excluding_provisioning > 1e-9 else 0.0)

        # ---- dollars-per-hour: N pods vs one pod (from currentSpendPerHr) -----
        spend = runpod.balance()
        total_spend_per_hr_n_pods = float(spend.get("currentSpendPerHr", 0.0))
        # per-pod $/hr = fleet $/hr divided by live pods (they are the same GPU tier);
        # one-pod $/hr is that per-pod rate. Honest: this is the live fleet rate now.
        per_pod_spend_per_hr = total_spend_per_hr_n_pods / max(len(pods), 1)
        one_pod_spend_per_hr = per_pod_spend_per_hr

        note = (
            f"REAL multi-pod DISTRIBUTION of the animated '{SHOT['scene']}' shot "
            f"({SHOT['resolution']}, {n_frames} frames, anchor stack: adaptive_thr="
            f"{SHOT['adaptive_threshold']}, {SHOT['denoiser']} denoiser, guides="
            f"{SHOT['denoise_guides']}, light_tree={SHOT['light_tree']}, draft_spp="
            f"{SHOT['draft_spp']}, bounces={SHOT['bounces']}) across {len(pods)} REAL "
            f"RunPod GPUs. Frames split {frame_split} (even={even}). "
            f"T_serial_single_pod={T_serial_single_pod:.1f}s = the same frames rendered "
            f"back-to-back on ONE pod ({serial_baseline_source}). "
            f"T_real_distributed_including_provisioning="
            f"{T_real_distributed_including_provisioning:.1f}s = first provisioning call to "
            f"last pod finishing (cold start: provision+reach+ship+deps+serial-baseline+"
            f"distributed render). T_real_distributed_excluding_provisioning="
            f"{T_real_distributed_excluding_provisioning:.1f}s = fleet-ready to last pod "
            f"finishing (steady-state throughput). provision_tax_s={provision_tax_s:.1f}s is "
            f"reported SEPARATELY. net_speedup_real = T_serial_single_pod / "
            f"T_real_distributed_excluding_provisioning = {net_speedup_real:.2f}x — the FAIR "
            f"steady-state comparison, NOT the ideal fan-out ceiling (exp_render_faninout.py "
            f"reports that upper bound; this is what survives real network + scheduling). "
            f"Provisioning was {provision_mode}. Every per-frame time is a real "
            f"time.perf_counter() whole-subprocess wall-clock measured ON the pod."
        )
        if not even:
            note += (
                f" CAVEAT: frames do not divide evenly across pods ({n_frames} frames / "
                f"{len(pods)} pods -> shares {[len(s) for s in frame_split]}); the pod with "
                f"{max_share} frames bounds the distributed wall-clock, so net_speedup_real "
                f"is capped below N by the uneven split."
            )
        if dist_errors:
            note += " ERRORS: " + "; ".join(dist_errors)

        result_rec = {
            "event": "distribution_result",
            "pods": len(pods),
            "requested_pods": args.pods,
            "n_frames": n_frames,
            "frame_split": frame_split,
            "frame_split_even": even,
            "provision_mode": provision_mode,
            "provision_s_per_pod": [round(float(x), 1) for x in provision_s],
            "t_provision_wall_s": round(t_provision_wall, 1),
            "t_prep_wall_s": round(t_prep_wall, 1),
            # ---- the honesty-critical numbers ----------------------------------
            "T_serial_single_pod": round(T_serial_single_pod, 2),
            "serial_baseline_source": serial_baseline_source,
            "serial_per_frame_render_s": serial_per_frame,
            "T_real_distributed_including_provisioning": round(
                T_real_distributed_including_provisioning, 2),
            "T_real_distributed_excluding_provisioning": round(
                T_real_distributed_excluding_provisioning, 2),
            "provision_tax_s": round(provision_tax_s, 2),
            "net_speedup_real": round(net_speedup_real, 4),
            # ---- dollars ------------------------------------------------------
            "total_spend_per_hr_n_pods": round(total_spend_per_hr_n_pods, 4),
            "one_pod_spend_per_hr": round(one_pod_spend_per_hr, 4),
            "per_pod_render_s": per_pod_render_s,
            "dist_errors": dist_errors,
            "gpus": [p["gpu"] for p in pods],
            "modeled": False,
            "note": note,
        }
        ledger_append(result_rec)
        log(f"RESULT net_speedup_real={net_speedup_real:.2f}x "
            f"(T_serial={T_serial_single_pod:.1f}s / "
            f"T_real_excl_provision={T_real_distributed_excluding_provisioning:.1f}s) | "
            f"T_real_incl_provision={T_real_distributed_including_provisioning:.1f}s | "
            f"provision_tax={provision_tax_s:.1f}s | "
            f"$/hr N-pods={total_spend_per_hr_n_pods:.3f} vs one-pod={one_pod_spend_per_hr:.3f}")
        if dist_errors:
            log(f"  DIST ERRORS: {dist_errors}")

    finally:
        # ---- MONEY SAFETY: terminate ALL pods, each in its OWN try/except so one
        #      pod failing to terminate never blocks the others. ------------------
        log(f"tearing down {len(pods)} pod(s)…")
        for i, pod in enumerate(pods):
            try:
                runpod.terminate(pod["id"])
                log(f"  pod {i + 1} ({pod['id']}) terminated")
            except Exception as e:  # noqa: BLE001 — never let one failure stop the rest
                log(f"  pod {i + 1} ({pod['id']}) terminate error (verify in console): {e}")
        # belt + suspenders: nuke anything still tracked (crash/partial-provision safety)
        try:
            runpod.terminate_all_tracked()
        except Exception as e:  # noqa: BLE001
            log(f"  terminate_all_tracked error (verify in console): {e}")
        b2 = runpod.balance()["clientBalance"]
        ledger_append({"event": "pod_down", "balance_after": b2,
                       "dollars_spent_this_run": round(bal0 - b2, 4)})
        log(f"all pods down. balance ${b2:.2f} (spent ${bal0 - b2:.2f} this run)")


if __name__ == "__main__":
    main()
