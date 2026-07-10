#!/usr/bin/env python3
"""run_scene_sweep.py — the FULL proven strict-delivery pipeline over a SCENE MATRIX.

GENERALIZATION_PLAN_2026-07-10.md branch A. The strict-delivery recipe that earned
DECISION=GROW on classroom (anchor stack + aov_edge selector + match-reference RAW
repair + per-frame repair budget — CONSOLIDATION_PLAN_2026-07-09.md RESULT section)
is run UNCHANGED over N diverse scenes on ONE pod, sequentially:

    one provision -> arm watchdog -> setup once -> scene 1 .. scene N -> teardown

so pod provisioning + Blender bootstrap are amortized across the whole matrix.
Objective #1 of the plan: the recipe must transfer WITHOUT per-scene tuning — every
scene therefore runs the IDENTICAL config (same spp, same selector, same budget,
same cam_motion); only the scene itself differs. A scene that fails is a REAL
NEGATIVE: it is ledgered as pruned and the sweep continues (use --fail-fast to stop).

Money-safety (same posture as run_integrated_production_benchmark.py, which this
driver forks and imports):
  * register_cleanup() BEFORE any provisioning; atexit + SIGINT/SIGTERM teardown.
  * refuses to start if any tracked/live pod exists (one pod driver at a time).
  * balance floor AND a 2.5x-of-quote headroom check before spending a cent.
  * monotonic GPU policy ladder (A100 -> H100 -> H200; Blackwell rejected — the
    parse_gpu_plan Blackwell guard is imported, not reimplemented).
  * arm_remote_watchdog() FIRST thing after provisioning, TTL sized to the sweep.
  * the long render runs DETACHED on the pod (runpod.ssh_detached) — short
    reconnecting polls, so an SSH reset cannot kill a healthy render.
  * finally: terminate_all_tracked() + verify .tracked_pods.json is empty.
  * --dry-run prints the manifest + the per-scene cost quote and exits ($0).

Receipts: one per scene, appended to
    docs/speed-lane-reports/spec-lab/scene_sweep_ledger.jsonl
(event "scene_sweep_receipt"), same RenderSpecReceipt shape as the capstone ledger
so calibrate_repair_budget.py can read BOTH ledgers.

Cost basis (printed up front, per scene): MEASURED anchors from the campaign
ledgers (see COST_BASIS below); the repair increment is MODELED from the measured
4K repair receipts scaled by pixel area — every quoted number is labeled.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))

import cx_integrated_speculation as integrated  # noqa: E402
import run_integrated_production_benchmark as production  # noqa: E402
import run_spec_render_token_end_to_end as end_to_end  # noqa: E402
import runpod  # noqa: E402

LEDGER = REPO / "docs/speed-lane-reports/spec-lab/scene_sweep_ledger.jsonl"

# ---------------------------------------------------------------------------
# SCENE MATRIX — only scenes PROVEN to resolve through pod/exp_render_stack.py's
# resolve_scene() (native SCENE_SOURCES key, or a direct .zip URL it fetches and
# whose largest .blend it picks). Verification evidence per scene is recorded so
# nobody has to re-derive why a scene is trusted. Scenes are DIVERSE on purpose:
# interior many-light GI / studio glossy-specular / sun-lit archviz exterior.
# ---------------------------------------------------------------------------
SCENE_MATRIX: list[dict[str, Any]] = [
    {
        "key": "classroom",
        "scene_arg": "classroom",  # native SCENE_SOURCES key in exp_render_stack.py
        "family": "interior_many_light_gi",
        "verified": (
            "MEASURED — the proven control: 1080p kf=1 receipt 2026-07-09 (H100, "
            "2.72x @ wt 0.8652 pre-repair) and the 4K strict-delivery GROW capstone "
            "2026-07-10 (integrated_spec_render_token_ledger.jsonl)."
        ),
    },
    {
        "key": "bmw27",
        "scene_arg": "bmw27",  # native SCENE_SOURCES key in exp_render_stack.py
        "family": "studio_glossy_specular",
        "verified": (
            "MEASURED — rendered end-to-end 2026-07-07 on a live L40S through the "
            "sibling runner exp_cycles_render_prod.py (same resolve_scene table): "
            "cross_scene_ledger.jsonl bmw27-contender 3.0995x @ worst-tile 0.9783."
        ),
    },
    {
        "key": "pavilion",
        # Direct .zip URL — resolve_scene() hashes the URL, unzips, and picks the
        # only/largest .blend (3d/pavillon_barcelone_v1.2.blend).
        "scene_arg": "https://download.blender.org/demo/test/pabellon_barcelona_v1.scene_.zip",
        "family": "archviz_sun_exterior",
        "verified": (
            "LOCAL 2026-07-10 — URL HTTP 200 (24,661,092 bytes); zip contains exactly "
            "one .blend; opened in local Blender 4.2.1: engine already CYCLES, active "
            "camera 'Camera' present (7 cameras total, 102 objects), and a real 64x64 "
            "1spp Cycles render completed (CX_RENDER_DONE t=0.6s). Not yet rendered "
            "on a CUDA pod — that is exactly what this sweep measures."
        ),
    },
]

# Scenes considered and EXCLUDED, with the measured reason (honesty ledger — a
# scene the pipeline structurally cannot ingest is documented, not forced):
EXCLUDED_SCENES: dict[str, str] = {
    "junkshop": (
        "no stable direct CC0 URL on download.blender.org/demo/test/ — "
        "resolve_scene() itself hard-codes a fallback-to-classroom for it, so "
        "requesting it would silently re-run the control scene"
    ),
    "fishy_cat": "HTTP 404 at download.blender.org/demo/test/fishy_cat.zip (checked 2026-07-10)",
    "koro": "HTTP 404 at download.blender.org/demo/test/koro.zip (checked 2026-07-10)",
    "barbershop_interior": (
        "HTTP 404 at download.blender.org/demo/test/barbershop_interior.zip "
        "(checked 2026-07-10)"
    ),
}

# ---------------------------------------------------------------------------
# COST BASIS — every constant here is a MEASURED number from a named ledger row;
# the quote arithmetic that combines them is MODELED and labeled as such.
# ---------------------------------------------------------------------------
COST_BASIS = {
    # MEASURED: integrated ledger 2026-07-09T17:30 (A100 SECURE, classroom 1080p,
    # ref 1536 / draft 192): per-frame reference render + per-anchor render.
    "a100_1080p_ref_per_frame_s": 105.65,
    "a100_1080p_anchor_per_frame_s": 27.14,
    "a100_1080p_fixed_overhead_s": 4.51,
    # MEASURED: integrated ledger 2026-07-10T09:31 (H100 SECURE, classroom 4K GROW):
    # per-frame aov_edge scoring and feathered-composite wall-clock at 3840x2160.
    "h100_4k_scoring_per_frame_s": 8.69,
    "h100_4k_composite_per_frame_s": 26.54,
    # MEASURED: A100/H100 per-frame reference ratio 1.48 (1080p) - 1.64 (4K); the
    # quote uses the A100 1080p base directly, so no cross-GPU scaling is applied
    # at the default resolution. Non-A100 landings only make the quote CONSERVATIVE.
    # MEASURED: pod setup, paid once per pod (Blender 4.2 tarball download+unpack +
    # pip deps + first-probe headroom), from the 2026-07-09 campaign runs.
    "pod_setup_once_s": 420.0,
    "scene_download_s": 45.0,       # MODELED: 6-70 MB zips on pod bandwidth
    "grading_io_misc_per_scene_s": 250.0,  # MODELED: PASS-4 SSIM + EXR IO + calib render
    # Plan cost basis rate (docs/research/GENERALIZATION_PLAN_2026-07-10.md):
    "a100_secure_usd_per_hr": 1.42,
    "h100_secure_usd_per_hr": 3.02,
    "grading_grid": 8,              # 8x8 grading tiles (exp_render_stack.py)
    "repair_margin_px": 16,
}


def log(message: str) -> None:
    print(f"[scene-sweep {time.strftime('%H:%M:%S')}] {message}", flush=True)


def build_sweep_config(scene_entry: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """The proven strict-delivery recipe, parameterized ONLY by the scene.

    Pure function (unit-tested). Field-for-field this is the GROW capstone config
    (integrated ledger 2026-07-10T09:31) at the sweep's resolution/spp scale:
    anchor stack (adaptive + OIDN + guides + light-tree) + aov_edge selector +
    match-reference RAW repair (repair_denoiser=none, repair_spp=ref_spp) +
    per-frame repair budget. NO per-scene knob differs across scenes.
    """
    config = {
        "scene": scene_entry["scene_arg"],
        "resolution": args.resolution,
        "frames": args.frames,
        "keyframe_every": 1,          # all-anchor: zero reprojection, modeled=false
        "draft_spp": args.draft_spp,
        "ref_spp": args.ref_spp,
        "adaptive_threshold": 0.02,
        "denoiser": "oidn",
        "denoise_guides": True,
        "light_tree": True,
        "bounces": 12,
        "hole_fill": "rerender",      # irrelevant at kf=1 (no holes) but kept identical
        "cam_motion": 1.0,            # identical across scenes — no per-scene tuning
        "seed": 0,
        "device": "GPU",
        "require_gpu": True,
        # sm_90 first-render JIT headroom (see run_integrated_production_benchmark.py).
        "gpu_probe_timeout_s": 1500,
        # ---- the repair loop exactly as the GROW capstone ran it ----
        "repair_enabled": True,
        "repair_selector": "aov_edge",
        "repair_denoiser": "none",           # match-reference RAW repair
        "repair_spp": args.ref_spp,          # true mini-reference tiles
        "repair_top_k": args.repair_top_k,
        "repair_max_per_frame": args.repair_max_per_frame,
        # Default 0.0 = rank-only, the PROVEN recipe: banks the full per-tile
        # (score, ssim_pre, ssim_after) set every scene, which is exactly the data
        # calibrate_repair_budget.py needs to validate the L1 threshold cross-scene
        # at zero extra runs. Pass --repair-min-divergence <calibrated> to apply
        # the L1 floor once it is validated.
        "repair_min_divergence": args.repair_min_divergence,
    }
    return config


def tile_repair_area_fraction(resolution: str, tiles_per_frame: int) -> float:
    """MODELED: fraction of the frame the bordered repair renders cover."""
    w, h = (int(v) for v in resolution.lower().split("x"))
    grid = COST_BASIS["grading_grid"]
    margin = COST_BASIS["repair_margin_px"]
    tile_w = w / grid + 2 * margin
    tile_h = h / grid + 2 * margin
    return min(1.0, tiles_per_frame * tile_w * tile_h / float(w * h))


def estimate_scene_cost(config: dict[str, Any], first_scene: bool = False) -> dict[str, Any]:
    """Per-scene wall-clock + $ quote from the MEASURED cost basis.

    Pure function (unit-tested). Anchored on the A100 1080p ref1536/draft192
    measurements; other resolutions/spp are scaled by pixel-count and spp ratios
    (Cycles trace cost is ~linear in both at fixed bounces) — those scalings are
    MODELED. The quote deliberately uses the A100 rate+times: the ladder's first
    rung is the cheapest AND slowest measured box, so landing higher only beats
    the quote.
    """
    basis = COST_BASIS
    w, h = (int(v) for v in config["resolution"].lower().split("x"))
    px_ratio = (w * h) / float(1920 * 1080)
    ref_ratio = config["ref_spp"] / 1536.0
    draft_ratio = config["draft_spp"] / 192.0
    frames = int(config["frames"])

    ref_s = basis["a100_1080p_ref_per_frame_s"] * px_ratio * ref_ratio * frames
    anchor_s = basis["a100_1080p_anchor_per_frame_s"] * px_ratio * draft_ratio * frames
    # Repair: bordered RAW renders at ref_spp over the selected-tile area, charged
    # like the reference trace scaled by covered area, plus per-frame fixed overhead,
    # scoring, and composite (4K-measured, pixel-scaled). MODELED.
    tiles_per_frame = min(
        int(config["repair_max_per_frame"]),
        max(1, int(config["repair_top_k"]) // max(1, frames)),
    )
    area_frac = tile_repair_area_fraction(config["resolution"], tiles_per_frame)
    ref_trace_per_frame = max(
        basis["a100_1080p_ref_per_frame_s"] * px_ratio * ref_ratio
        - basis["a100_1080p_fixed_overhead_s"],
        0.0,
    )
    repair_render_s = (
        ref_trace_per_frame * area_frac + basis["a100_1080p_fixed_overhead_s"]
    ) * frames
    scoring_s = basis["h100_4k_scoring_per_frame_s"] * (px_ratio / 4.0) * frames
    composite_s = basis["h100_4k_composite_per_frame_s"] * (px_ratio / 4.0) * frames
    overhead_s = basis["scene_download_s"] + basis["grading_io_misc_per_scene_s"]
    setup_s = basis["pod_setup_once_s"] if first_scene else 0.0

    total_s = ref_s + anchor_s + repair_render_s + scoring_s + composite_s + overhead_s + setup_s
    usd = total_s / 3600.0 * basis["a100_secure_usd_per_hr"]
    return {
        "label": "MODELED quote from MEASURED per-stage anchors (see COST_BASIS)",
        "ref_render_s": round(ref_s, 1),
        "anchor_render_s": round(anchor_s, 1),
        "repair_render_s_modeled": round(repair_render_s, 1),
        "selector_composite_s_modeled": round(scoring_s + composite_s, 1),
        "scene_overhead_s_modeled": round(overhead_s, 1),
        "pod_setup_once_s": round(setup_s, 1),
        "total_s": round(total_s, 1),
        "total_minutes": round(total_s / 60.0, 1),
        "usd_at_a100_secure": round(usd, 2),
    }


def build_sweep_manifest(args: argparse.Namespace) -> dict[str, Any]:
    """Everything the sweep will do, as data — printed by --dry-run, ledgered on run."""
    wanted = [s.strip() for s in args.scenes.split(",") if s.strip()]
    by_key = {entry["key"]: entry for entry in SCENE_MATRIX}
    unknown = [k for k in wanted if k not in by_key]
    if unknown:
        raise ValueError(
            f"unknown scene key(s) {unknown}; known: {sorted(by_key)} "
            f"(excluded, with reasons: {sorted(EXCLUDED_SCENES)})"
        )
    scenes = []
    total_usd = 0.0
    total_s = 0.0
    for i, key in enumerate(wanted):
        entry = by_key[key]
        config = build_sweep_config(entry, args)
        quote = estimate_scene_cost(config, first_scene=(i == 0))
        total_usd += quote["usd_at_a100_secure"]
        total_s += quote["total_s"]
        scenes.append({"scene": entry, "config": config, "quote": quote})
    gpu_plan = production.parse_gpu_plan(args.gpu_plan)
    per_scene_timeout_s = args.max_minutes_per_scene * 60
    watchdog_ttl_s = 1800 + len(scenes) * per_scene_timeout_s + 1800
    return {
        "scenes": scenes,
        "gpu_plan": gpu_plan,
        "per_scene_timeout_s": per_scene_timeout_s,
        "watchdog_ttl_s": watchdog_ttl_s,
        "quote_total_s": round(total_s, 1),
        "quote_total_usd_at_a100_secure": round(total_usd, 2),
        "quote_label": "MODELED from MEASURED anchors; A100-SECURE rate (ladder floor)",
        "excluded_scenes": EXCLUDED_SCENES,
    }


def write_scene_receipt(
    args: argparse.Namespace,
    scene_entry: dict[str, Any],
    config: dict[str, Any],
    metrics: dict[str, Any],
    pod: dict[str, Any],
    sweep_id: str,
) -> dict[str, Any]:
    """Same RenderSpecReceipt shape as run_integrated_production_benchmark.write_receipt,
    appended to the SWEEP ledger (never touching the capstone report/ledger)."""
    job = integrated.RenderSpecJob(
        job_id=f"{sweep_id}-{scene_entry['key']}",
        workload="production_video" if config["frames"] > 1 else "production_still",
        scene=scene_entry["key"],
        resolution=config["resolution"],
        frames=config["frames"],
        render_policy="anchor + aov_edge select + match-reference raw repair (scene sweep)",
        token_policy="json-template job-event draft->byte verify->repair",
    )
    token = end_to_end.run_manifest_speculation(
        end_to_end.manifest_stream(job, args.events), args.prefix_rows,
        [4096, 2048, 1536, 1024, 768, 512, 384, 256, 128, 64, 32, 16, 8, 4, 2],
    )
    render = end_to_end.normalize_stack_metrics(metrics, f"runpod:{pod['id']}")
    decision = integrated.RenderVerifier.decide(
        token_exact=bool(token["exact"]), global_ssim=render["global_ssim"],
        worst_tile_ssim=render["worst_tile_ssim"], render_modeled=render["render_modeled"],
    )
    receipt = integrated.RenderSpecReceipt(
        job=job,
        token_baseline_s=float(token["baseline_s"]), token_spec_s=float(token["spec_s"]),
        render_baseline_s=render["render_baseline_s"], render_spec_s=render["render_spec_s"],
        global_ssim=render["global_ssim"], worst_tile_ssim=render["worst_tile_ssim"],
        token_exact=bool(token["exact"]), render_modeled=render["render_modeled"],
        evidence_type=render["evidence_type"], decision=decision,
    ).to_dict()
    receipt["token"] = token
    receipt["render_metrics"] = metrics
    receipt["gpu"] = {key: pod.get(key) for key in ("gpu", "cloud", "cuda_driver_version")}
    receipt["scene_key"] = scene_entry["key"]
    receipt["scene_family"] = scene_entry["family"]
    receipt["sweep_id"] = sweep_id
    end_to_end.append_jsonl(LEDGER, {"event": "scene_sweep_receipt", "receipt": receipt})
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sweep-id", default=f"cx-scene-sweep-{time.strftime('%Y%m%d')}")
    parser.add_argument(
        "--scenes", default=",".join(entry["key"] for entry in SCENE_MATRIX),
        help="comma-separated scene keys from SCENE_MATRIX (default: all)",
    )
    parser.add_argument("--resolution", default="1920x1080")
    parser.add_argument("--frames", type=int, default=4)
    parser.add_argument("--draft-spp", type=int, default=192)
    parser.add_argument("--ref-spp", type=int, default=1536)
    parser.add_argument("--repair-top-k", type=int, default=32)
    parser.add_argument("--repair-max-per-frame", type=int, default=8)
    parser.add_argument(
        "--repair-min-divergence", type=float, default=0.0,
        help="aov_edge score floor (L1). Default 0.0 = rank-only (the PROVEN GROW "
             "recipe; banks full calibration data). Apply the calibrated value from "
             "calibrate_repair_budget.py only after the sweep validates it cross-scene.",
    )
    parser.add_argument("--events", type=int, default=1024)
    parser.add_argument("--prefix-rows", type=int, default=12)
    parser.add_argument("--max-minutes-per-scene", type=int, default=60)
    parser.add_argument("--min-balance", type=float, default=6.0)
    parser.add_argument("--gpu-plan", default=production.DEFAULT_GPU_PLAN)
    parser.add_argument(
        "--fail-fast", action="store_true",
        help="abort the sweep on the first scene failure (default: ledger it as "
             "pruned and continue — a real negative on one scene must not cost the "
             "other scenes' receipts)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    manifest = build_sweep_manifest(args)
    print(json.dumps(
        {
            "sweep_id": args.sweep_id,
            "per_scene_quotes": [
                {
                    "scene": s["scene"]["key"],
                    "family": s["scene"]["family"],
                    **s["quote"],
                }
                for s in manifest["scenes"]
            ],
            "quote_total_usd_at_a100_secure": manifest["quote_total_usd_at_a100_secure"],
            "quote_total_minutes": round(manifest["quote_total_s"] / 60.0, 1),
            "quote_label": manifest["quote_label"],
        },
        indent=2, sort_keys=True,
    ))
    if args.dry_run:
        print(json.dumps({"dry_run_manifest": manifest}, indent=2, sort_keys=True))
        return

    runpod.register_cleanup()
    tracked = runpod._load_tracked()
    live = runpod.live_pods()
    balance = runpod.balance()
    if tracked or live:
        raise RuntimeError(f"refusing sweep with existing pods: tracked={tracked}, live={live}")
    funds = float(balance["clientBalance"])
    quote = float(manifest["quote_total_usd_at_a100_secure"])
    if funds <= args.min_balance:
        raise RuntimeError(f"balance ${funds:.2f} is below floor ${args.min_balance:.2f}")
    if funds < 2.5 * quote:
        raise RuntimeError(
            f"balance ${funds:.2f} is below 2.5x the sweep quote (${quote:.2f}); "
            f"trim --scenes or top up — never bet the whole balance on one run"
        )
    preflight = {
        "sweep_id": args.sweep_id,
        "tracked": tracked,
        "live": live,
        "balance": balance,
        "manifest": manifest,
    }
    end_to_end.append_jsonl(LEDGER, {"event": "scene_sweep_preflight", "preflight": preflight})
    log(f"preflight clear; balance ${funds:.2f}; quote ${quote:.2f}; provisioning GPU ladder")
    try:
        pod = runpod.provision_reachable(
            manifest["gpu_plan"], production.POD_IMAGE,
            disk_gb=production.POD_DISK_GB, name=args.sweep_id,
        )
    except Exception as exc:
        end_to_end.append_jsonl(
            LEDGER,
            {"event": "scene_sweep_provision_pruned", "reason": str(exc), "preflight": preflight},
        )
        raise
    results: list[dict[str, Any]] = []
    try:
        runpod.arm_remote_watchdog(pod, manifest["watchdog_ttl_s"])
        log(f"watchdog armed ttl={manifest['watchdog_ttl_s']}s on {pod['gpu']} ({pod['cloud']})")
        rc, _, err = runpod.ssh(pod, "mkdir -p /root/spec-lab", timeout=60)
        if rc != 0:
            raise RuntimeError(f"remote mkdir failed: {err[-200:]}")
        ok, error = runpod.scp_to(pod, str(HERE / "pod"), "/root/spec-lab/")
        if not ok:
            raise RuntimeError(f"pod runner transfer failed: {error[-200:]}")
        rc, out, err = runpod.ssh(
            pod,
            "pip install --break-system-packages --no-cache-dir "
            "OpenEXR Imath imageio numpy pillow scikit-image 2>&1 | tail -2",
            timeout=600,
        )
        if rc != 0:
            raise RuntimeError(f"remote dependencies failed: {(out + err)[-300:]}")
        for item in manifest["scenes"]:
            entry, config = item["scene"], item["config"]
            log(f"scene {entry['key']} start (quote {item['quote']['usd_at_a100_secure']}$"
                f" / {item['quote']['total_minutes']}min)")
            started = time.time()
            metrics = production.remote_stack(pod, config, manifest["per_scene_timeout_s"])
            elapsed = time.time() - started
            if metrics.get("error"):
                end_to_end.append_jsonl(
                    LEDGER,
                    {
                        "event": "scene_sweep_scene_pruned",
                        "sweep_id": args.sweep_id,
                        "scene": entry["key"],
                        "config": config,
                        "reason": str(metrics["error"])[:2000],
                        "wall_s": round(elapsed, 1),
                        "pod": {k: pod.get(k) for k in ("id", "gpu", "cloud")},
                    },
                )
                log(f"scene {entry['key']} PRUNED (real negative, ledgered): "
                    f"{str(metrics['error'])[:200]}")
                if args.fail_fast:
                    raise RuntimeError(f"--fail-fast: scene {entry['key']} failed")
                continue
            receipt = write_scene_receipt(args, entry, config, metrics, pod, args.sweep_id)
            results.append(receipt)
            log(
                f"scene {entry['key']} DONE in {elapsed/60.0:.1f}min: "
                f"speedup {receipt['end_to_end_speedup_x']:.3f}x, "
                f"global {receipt['global_ssim']:.4f}, "
                f"worst-tile {receipt['worst_tile_ssim']:.4f}, "
                f"decision {receipt['decision']['action']}"
            )
        summary = {
            "event": "scene_sweep_summary",
            "sweep_id": args.sweep_id,
            "scenes_completed": [r["scene_key"] for r in results],
            "table": [
                {
                    "scene": r["scene_key"],
                    "end_to_end_speedup_x": r["end_to_end_speedup_x"],
                    "global_ssim": r["global_ssim"],
                    "worst_tile_ssim": r["worst_tile_ssim"],
                    "decision": r["decision"]["action"],
                    "modeled": r["render_modeled"],
                }
                for r in results
            ],
        }
        end_to_end.append_jsonl(LEDGER, summary)
        print(json.dumps(summary, indent=2, sort_keys=True))
    finally:
        runpod.terminate_all_tracked()
        remaining = runpod._load_tracked()
        if remaining:
            raise RuntimeError(f"teardown incomplete; tracked pods remain: {remaining}")
        log("pod teardown confirmed")


if __name__ == "__main__":
    main()
