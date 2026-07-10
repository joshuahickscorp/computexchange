#!/usr/bin/env python3
"""
exp_distributed.py — Tracks B4 / C3: distributed "draft on the fleet, verify on the GPU".

The ComputeExchange edge: many cheap nodes produce speculative DRAFTS in parallel while
one (or few) GPUs stream the VERIFY/correct pass. This runner measures the two halves
FOR REAL on the pod and models only the fleet fan-out.

What is REAL (measured on this pod, per invocation):
  * draft_ms  : the draft step forced onto the CPU (numpy, single-node cost of one draft
                unit — a frame/tile of work a cheap fleet node would do).
  * verify_ms : the verify step on the GPU (a torch op on cuda: the correction/denoise/
                decode a real GPU would run). If no CUDA device is present we measure the
                verify on CPU torch and SAY SO in the note (still a real measurement, just
                not GPU-accelerated) — we never fabricate a GPU timing.

What is MODELED (and marked modeled:true + stated in the note):
  * fleet_parallelism : how many cheap nodes draft in parallel (default 8). This is an
                        assumption about fleet width, not a measurement.
  * the pipeline overlap: distributed_wall ≈ max(draft_ms / fleet_parallelism,
                          verify_ms_batched); single_node_wall ≈ draft_ms + verify_ms.
                          distributed_speedup = single_node_wall / distributed_wall.

Params (argv[1] JSON), all optional:
  modality          : "video" (default) | "render"   — shapes the synthetic work unit
  draft_where       : "cpu" (default)                — where the draft is timed
  verify_where      : "gpu" (default)                — where the verify is timed
  fleet_parallelism : int, modeled fleet width       (default 8)
  verify_batch      : int, verify units streamed per GPU launch (default 1). Batching
                      amortizes launch overhead, lowering per-unit verify time.
  work              : int, work-unit side length in px (default 512)
  n_units           : int, draft units to average timing over (default 8)
  seed              : rng seed                        (default 0)

Emits ONE json line on stdout:
  {"distributed_speedup","draft_ms","verify_ms","fleet_parallelism","modeled":true,"note"}

Contract: human logs -> stderr; last stdout line is exactly one JSON object; any failure
emits {"error":...} as the last stdout line and exits (never hangs, never crashes silent).
"""

import json
import sys
import time

import numpy as np


def log(*a):
    print("[distributed]", *a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# DRAFT — the cheap step a fleet node runs, forced onto CPU (numpy).           #
# --------------------------------------------------------------------------- #
def draft_cpu(modality, work, rng):
    """One draft unit on the CPU. Returns (array, elapsed_seconds).

    video  : a motion-compensated frame-interpolation draft = warp + blend two frames
             (cheap arithmetic a commodity CPU node can do).
    render : a low-spp tile draft = a couple of noisy sample passes averaged (the
             'render bits' cheap draft).
    Both are honest CPU numpy work; we time the compute, excluding allocation of inputs.
    """
    if modality == "render":
        base = rng.standard_normal((work, work, 3)).astype(np.float32)
        t0 = time.perf_counter()
        # a few cheap sample passes (low-spp draft) averaged — the draft "render".
        acc = np.zeros_like(base)
        for _ in range(4):
            acc += base + rng.standard_normal(base.shape).astype(np.float32) * 0.5
        out = acc * 0.25
        # a light box blur (separable) to mimic a cheap reconstruction pass.
        out = (out + np.roll(out, 1, 0) + np.roll(out, -1, 0)
               + np.roll(out, 1, 1) + np.roll(out, -1, 1)) * 0.2
        elapsed = time.perf_counter() - t0
    else:  # "video": frame-interpolation draft (warp + blend)
        f0 = rng.standard_normal((work, work, 3)).astype(np.float32)
        f1 = rng.standard_normal((work, work, 3)).astype(np.float32)
        t0 = time.perf_counter()
        # integer-shift "optical-flow warp" of f1, then blend with f0 (the interp draft).
        warped = np.roll(f1, shift=2, axis=1)
        out = 0.5 * f0 + 0.5 * warped
        # residual-style refinement pass (cheap gradient add).
        gx = np.diff(out, axis=1, prepend=out[:, :1, :])
        out = out + 0.1 * gx
        elapsed = time.perf_counter() - t0
    return out, elapsed


# --------------------------------------------------------------------------- #
# VERIFY — the GPU step (torch on cuda; CPU-torch fallback measured honestly). #
# --------------------------------------------------------------------------- #
def verify_gpu(modality, work, verify_batch, seed):
    """Time the verify/correct step on the GPU. Returns (per_unit_seconds, device_str).

    The verify op stands in for the GPU correction pass (a small conv-style / decode
    workload): a batched 2D operation over `verify_batch` units. We synchronize cuda so
    the timing is real wall-time, not just kernel-launch return. per_unit_seconds is the
    batched time divided by verify_batch (batching amortizes launch overhead).
    """
    import torch

    torch.manual_seed(seed)
    if torch.cuda.is_available():
        device = "cuda"
    else:
        device = "cpu"
        log("no CUDA device on this pod — verify measured on CPU torch (reported in note).")

    b = max(1, int(verify_batch))
    ch = 3
    # a batch of work units; the verify op is a couple of depthwise-ish convolutions +
    # a nonlinearity — representative of a lightweight correction/decode head.
    x = torch.randn(b, ch, work, work, device=device)
    weight = torch.randn(ch, 1, 3, 3, device=device)

    def _sync():
        if device == "cuda":
            torch.cuda.synchronize()

    # warmup (kernel compile / caches) so the timed run reflects steady state.
    with torch.no_grad():
        for _ in range(3):
            y = torch.nn.functional.conv2d(x, weight, padding=1, groups=ch)
            y = torch.tanh(y)
            y = torch.nn.functional.conv2d(y, weight, padding=1, groups=ch)
        _sync()

        reps = 10
        t0 = time.perf_counter()
        for _ in range(reps):
            y = torch.nn.functional.conv2d(x, weight, padding=1, groups=ch)
            y = torch.tanh(y)
            y = torch.nn.functional.conv2d(y, weight, padding=1, groups=ch)
        _sync()
        batch_elapsed = (time.perf_counter() - t0) / reps

    per_unit = batch_elapsed / b
    return per_unit, device


# --------------------------------------------------------------------------- #
# main                                                                         #
# --------------------------------------------------------------------------- #
def main():
    params = json.loads(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else {}
    modality = str(params.get("modality", "video"))
    draft_where = str(params.get("draft_where", "cpu"))
    verify_where = str(params.get("verify_where", "gpu"))
    fleet_parallelism = int(params.get("fleet_parallelism", 8))
    verify_batch = int(params.get("verify_batch", 1))
    work = int(params.get("work", 512))
    n_units = int(params.get("n_units", 8))
    seed = int(params.get("seed", 0))

    fleet_parallelism = max(1, fleet_parallelism)
    verify_batch = max(1, verify_batch)
    n_units = max(1, n_units)
    rng = np.random.default_rng(seed)

    log(f"modality={modality} draft_where={draft_where} verify_where={verify_where} "
        f"fleet_parallelism={fleet_parallelism} verify_batch={verify_batch} "
        f"work={work} n_units={n_units}")

    # ---- REAL: measure the draft step on CPU, averaged over n_units --------- #
    # one untimed warmup to prime numpy/CPU caches, then average the timed runs.
    draft_cpu(modality, work, rng)
    draft_times = []
    for _ in range(n_units):
        _, dt = draft_cpu(modality, work, rng)
        draft_times.append(dt)
    draft_s = float(np.median(draft_times))   # median = robust to scheduler jitter
    log(f"draft (CPU numpy) median = {draft_s*1000:.2f} ms over {n_units} units")

    # ---- REAL: measure the verify step on GPU (or CPU-torch fallback) ------- #
    try:
        verify_s, verify_device = verify_gpu(modality, work, verify_batch, seed)
    except Exception as e:
        # torch missing/broken is a hard fail for THIS runner's metric -> honest error.
        log(f"verify measurement failed: {e}")
        print(json.dumps({"error": f"verify step failed: {type(e).__name__}: {e}"}))
        sys.exit(0)
    log(f"verify (torch/{verify_device}, batch={verify_batch}) per-unit = {verify_s*1000:.2f} ms")

    # ---- MODELED: the distributed pipeline overlap -------------------------- #
    # single-node: draft then verify, serially, on one box.
    single_node_s = draft_s + verify_s
    # distributed: N cheap nodes draft in parallel while the GPU streams verify. The
    # steady-state wall-clock per unit is the SLOWER of (draft fanned across the fleet)
    # and (batched GPU verify) — the two stages overlap in a pipeline.
    distributed_s = max(draft_s / fleet_parallelism, verify_s)
    distributed_speedup = single_node_s / max(distributed_s, 1e-9)

    # which stage is the bottleneck (useful signal for the ledger / next remediation).
    bottleneck = "verify(GPU)" if verify_s >= draft_s / fleet_parallelism else "draft(fleet)"
    log(f"single_node={single_node_s*1000:.2f}ms distributed={distributed_s*1000:.2f}ms "
        f"-> distributed_speedup={distributed_speedup:.3f} (bottleneck={bottleneck})")

    note = (f"draft_ms (CPU numpy) and verify_ms (torch/{verify_device}) are REAL measured "
            f"on this pod; fleet_parallelism={fleet_parallelism} and the pipeline-overlap "
            f"model (distributed_wall=max(draft/{fleet_parallelism}, verify_batched)) are "
            f"MODELED. Bottleneck stage: {bottleneck}.")
    if verify_device != "cuda":
        note += " NOTE: no CUDA on pod — verify timed on CPU torch, so the GPU advantage is UNDERSTATED."

    metrics = {
        "distributed_speedup": round(float(distributed_speedup), 4),
        "draft_ms": round(float(draft_s * 1000.0), 3),
        "verify_ms": round(float(verify_s * 1000.0), 3),
        "fleet_parallelism": int(fleet_parallelism),
        "verify_batch": int(verify_batch),
        "verify_device": verify_device,
        "single_node_ms": round(float(single_node_s * 1000.0), 3),
        "distributed_ms": round(float(distributed_s * 1000.0), 3),
        "bottleneck": bottleneck,
        "modeled": True,
        "note": note,
    }
    # LAST stdout line == exactly one JSON object.
    print(json.dumps(metrics))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        sys.exit(0)
