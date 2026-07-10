#!/usr/bin/env python3
"""Measured Cycles tile/crop refinement on RunPod.

This runner replaces the speculative ladder's modeled "refine failed tiles"
branch with a real first proof:

    full low-spp draft -> score tiles -> crop-render failed tiles -> paste -> re-score

It uses the CX standalone Cycles batch-crop manifest path. Each crop render
keeps the camera at the original full-frame resolution while rendering only a
smaller BufferParams region, then pastes that region back into the draft.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

import cycles_fork  # noqa: E402
import runpod  # noqa: E402
from run_cycles_quality_ladder import (  # noqa: E402
    ADA_TAR,
    HOPPER_TAR,
    POD_DISK_GB,
    POD_IMAGE,
    WATCHDOG_TTL_S,
    default_tar_for_tier,
    gpu_plan_for_tier,
    safe_name,
    synthetic_scene_cmd,
    transfer_preflight,
    upload_prebuilt_root,
)


DATE = "2026-07-09"
LEDGER = os.path.join(
    REPO,
    "docs/speed-lane-reports/spec-lab/tile_refinement_ledger.jsonl",
)
REPORT = os.path.join(
    REPO,
    f"docs/speed-lane-reports/spec-lab/TILE_REFINEMENT_{DATE}.md",
)
DELIVERY_GLOBAL = 0.98
DELIVERY_WORST_TILE = 0.95
PREVIEW_GLOBAL = 0.90
PREVIEW_WORST_TILE = 0.85


def log(message: str) -> None:
    print(f"[tile-refine {time.strftime('%H:%M:%S')}] {message}", flush=True)


def append_ledger(record: dict) -> None:
    cycles_fork.append_ledger(record, ledger=LEDGER)


def parse_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def classify(global_ssim: float, worst_tile_ssim: float) -> str:
    if global_ssim >= DELIVERY_GLOBAL and worst_tile_ssim >= DELIVERY_WORST_TILE:
        return "delivery"
    if global_ssim >= PREVIEW_GLOBAL and worst_tile_ssim >= PREVIEW_WORST_TILE:
        return "preview"
    return "fail"


def tile_bounds(width: int, height: int, grid: int, xi: int, yi: int) -> tuple[int, int, int, int]:
    x0 = xi * width // grid
    x1 = (xi + 1) * width // grid
    y0 = yi * height // grid
    y1 = (yi + 1) * height // grid
    return x0, y0, x1, y1


def cx_crop_for_tile(
    width: int,
    height: int,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
) -> str:
    """Return CX crop CLI bounds for a top-left image tile.

    Cycles' render buffer uses bottom-up full_y coordinates, while PNG/PIL tile
    coordinates are top-left. The conversion is the only Y flip the runner needs.
    """
    crop_y = height - y1
    return f"{x0},{crop_y},{x1 - x0},{y1 - y0},{width},{height}"


def build_remote_script(config: dict) -> str:
    payload = json.dumps(config, sort_keys=True)
    return r"""
import json
import os
import subprocess
import time
from pathlib import Path

import numpy as np
from PIL import Image

CONFIG = json.loads(%r)

root = Path(CONFIG["root"])
binary = Path(CONFIG["binary"])
scene = CONFIG["scene"]
scene_path = root / "examples" / scene
scene_safe = CONFIG["scene_safe"]
prefix = Path("/tmp") / f"cx_tile_refine_{scene_safe}_{CONFIG['draft_samples']}_{CONFIG['refine_samples']}"
device = CONFIG["device"]
disable_adaptive = CONFIG["disable_adaptive_sampling"]
grid = int(CONFIG["grid"])
threshold = float(CONFIG["refine_threshold"])
max_tiles = int(CONFIG["max_refine_tiles"])

env = os.environ.copy()
env["LD_LIBRARY_PATH"] = str(root / "install" / "lib") + ":" + env.get("LD_LIBRARY_PATH", "")


def run(cmd, label):
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=root, env=env, text=True, capture_output=True)
    elapsed = time.time() - t0
    if proc.returncode != 0:
        print(f"CX_TILE_STAGE_FAIL={label}")
        print(proc.stdout[-4000:])
        print(proc.stderr[-4000:])
        raise SystemExit(proc.returncode)
    return elapsed, proc.stdout, proc.stderr


def cx_crop_for(bounds):
    full_width = int(CONFIG["width"])
    full_height = int(CONFIG["height"])
    x0, y0, x1, y1 = bounds
    return f"{x0},{full_height - y1},{x1 - x0},{y1 - y0},{full_width},{full_height}"


def render(samples, out_path, input_scene, crop=None):
    cmd = [str(binary)]
    if device:
        cmd.extend(["--device", device])
    if disable_adaptive:
        cmd.append("--disable-adaptive-sampling")
    cmd.extend(["--samples", str(samples), "--output", str(out_path)])
    if crop is not None:
        cmd.extend(["--cx-crop", crop])
    cmd.append(str(input_scene))
    elapsed, _out, _err = run(cmd, f"render_{samples}_{Path(out_path).name}")
    if not Path(out_path).is_file() or Path(out_path).stat().st_size <= 0:
        print(f"CX_TILE_STAGE_FAIL=missing_render_output_{Path(out_path).name}")
        raise SystemExit(1)
    return elapsed


def load_rgb(path):
    return np.asarray(Image.open(path).convert("RGB"), dtype=np.float64) / 255.0


def gray(arr):
    return 0.2126 * arr[..., 0] + 0.7152 * arr[..., 1] + 0.0722 * arr[..., 2]


def ssim(a, b):
    c1 = 0.01 ** 2
    c2 = 0.03 ** 2
    mux = float(a.mean())
    muy = float(b.mean())
    vx = float(((a - mux) ** 2).mean())
    vy = float(((b - muy) ** 2).mean())
    cov = float(((a - mux) * (b - muy)).mean())
    denom = (mux * mux + muy * muy + c1) * (vx + vy + c2)
    if denom == 0:
        return 1.0 if np.array_equal(a, b) else 0.0
    return ((2 * mux * muy + c1) * (2 * cov + c2)) / denom


def tile_bounds(width, height, grid, xi, yi):
    x0 = xi * width // grid
    x1 = (xi + 1) * width // grid
    y0 = yi * height // grid
    y1 = (yi + 1) * height // grid
    return x0, y0, x1, y1


def score_tiles(ref_g, img_g):
    h, w = ref_g.shape
    scores = []
    for yi in range(grid):
        for xi in range(grid):
            x0, y0, x1, y1 = tile_bounds(w, h, grid, xi, yi)
            scores.append({
                "xi": xi,
                "yi": yi,
                "x0": x0,
                "y0": y0,
                "x1": x1,
                "y1": y1,
                "ssim": float(ssim(ref_g[y0:y1, x0:x1], img_g[y0:y1, x0:x1])),
            })
    return scores


def summarize_quality(ref_rgb, img_rgb):
    ref_g = gray(ref_rgb)
    img_g = gray(img_rgb)
    tiles = score_tiles(ref_g, img_g)
    scores = np.asarray([t["ssim"] for t in tiles], dtype=np.float64)
    delta = np.abs(ref_rgb - img_rgb)
    global_ssim = float(ssim(ref_g, img_g))
    worst = float(scores.min()) if len(scores) else 1.0
    p5 = float(np.percentile(scores, 5)) if len(scores) else 1.0
    return {
        "quality": round(global_ssim, 9),
        "worst_tile_ssim": round(worst, 9),
        "p5_tile_ssim": round(p5, 9),
        "tile_count": int(len(tiles)),
        "png_mae": round(float(delta.mean()), 9),
        "png_maxe": round(float(delta.max()), 9),
        "tier": CONFIG["classify"](global_ssim, worst) if False else None,
        "tiles": tiles,
    }


def classify(global_ssim, worst_tile_ssim):
    if global_ssim >= 0.98 and worst_tile_ssim >= 0.95:
        return "delivery"
    if global_ssim >= 0.90 and worst_tile_ssim >= 0.85:
        return "preview"
    return "fail"


def render_crops_batch(tiles, label):
    if not tiles:
        return [], 0.0

    manifest = prefix.with_name(f"{prefix.name}_{label}_manifest.txt")
    results = []
    with open(manifest, "w") as f:
        for tile in tiles:
            bounds = (tile["x0"], tile["y0"], tile["x1"], tile["y1"])
            crop_spec = cx_crop_for(bounds)
            crop_png = prefix.with_name(
                f"{prefix.name}_{label}_x{tile['xi']}_y{tile['yi']}.png"
            )
            f.write(
                f"{crop_png} {CONFIG['refine_samples']} 0 {CONFIG['refine_samples']} "
                f"{crop_spec.replace(',', ' ')}\n"
            )
            results.append({
                "tile": tile,
                "cx_crop": crop_spec,
                "png": str(crop_png),
            })

    cmd = [str(binary)]
    if device:
        cmd.extend(["--device", device])
    if disable_adaptive:
        cmd.append("--disable-adaptive-sampling")
    cmd.extend(["--cx-batch-manifest", str(manifest), str(scene_path)])
    elapsed, _out, _err = run(cmd, f"render_crop_batch_{len(tiles)}")
    for result in results:
        out_path = Path(result["png"])
        if not out_path.is_file() or out_path.stat().st_size <= 0:
            print(f"CX_TILE_STAGE_FAIL=missing_crop_output_{out_path.name}")
            raise SystemExit(1)
        result["time_s"] = elapsed / float(len(results))
        result["batch_time_s"] = elapsed
    return results, elapsed


for path in Path("/tmp").glob(f"{prefix.name}*"):
    try:
        path.unlink()
    except OSError:
        pass

ref_png = prefix.with_name(prefix.name + "_ref.png")
draft_png = prefix.with_name(prefix.name + "_draft.png")
merged_png = prefix.with_name(prefix.name + "_merged.png")

print(f"CX_TILE_SCENE={scene}")
print(f"CX_TILE_REF_SAMPLES={CONFIG['ref_samples']}")
print(f"CX_TILE_DRAFT_SAMPLES={CONFIG['draft_samples']}")
print(f"CX_TILE_REFINE_SAMPLES={CONFIG['refine_samples']}")
print(f"CX_TILE_GRID={grid}")
print(f"CX_TILE_THRESHOLD={threshold}")

ref_time = render(CONFIG["ref_samples"], ref_png, scene_path)
draft_time = render(CONFIG["draft_samples"], draft_png, scene_path)

ref_rgb = load_rgb(ref_png)
draft_rgb = load_rgb(draft_png)
height, width, _ = ref_rgb.shape
if width != CONFIG["width"] or height != CONFIG["height"]:
    CONFIG["width"] = width
    CONFIG["height"] = height

draft_summary = summarize_quality(ref_rgb, draft_rgb)
draft_summary["tier"] = classify(draft_summary["quality"], draft_summary["worst_tile_ssim"])
failed = [t for t in draft_summary["tiles"] if t["ssim"] < threshold]
failed.sort(key=lambda t: t["ssim"])
selected = failed[:max_tiles]
selected_keys = {(t["xi"], t["yi"]) for t in selected}

orientation_probes = []
chosen_invert_y = None

crop_results = []
merged = draft_rgb.copy()
crop_results, crop_batch_time = render_crops_batch(selected, "refine")
for crop in crop_results:
    tile = crop["tile"]
    crop_rgb = load_rgb(crop["png"])
    expected_h = tile["y1"] - tile["y0"]
    expected_w = tile["x1"] - tile["x0"]
    if crop_rgb.shape[:2] != (expected_h, expected_w):
        raise SystemExit(
            f"crop shape mismatch for tile {tile}: {crop_rgb.shape[:2]} vs {(expected_h, expected_w)}"
        )
    ref_tile_g = gray(ref_rgb)[tile["y0"]:tile["y1"], tile["x0"]:tile["x1"]]
    crop["crop_vs_ref_tile_ssim"] = round(float(ssim(ref_tile_g, gray(crop_rgb))), 9)
    merged[tile["y0"]:tile["y1"], tile["x0"]:tile["x1"], :] = crop_rgb

Image.fromarray(np.clip(merged * 255.0 + 0.5, 0, 255).astype(np.uint8), "RGB").save(merged_png)

final_summary = summarize_quality(ref_rgb, merged)
final_summary["tier"] = classify(final_summary["quality"], final_summary["worst_tile_ssim"])
crop_product_time = crop_batch_time
orientation_probe_total = sum(float(p["time_s"]) for p in orientation_probes)
wrong_orientation_time = sum(
    float(p["time_s"]) for p in orientation_probes if bool(p.get("invert_y")) != chosen_invert_y
)
product_time = draft_time + crop_product_time
experiment_time = draft_time + orientation_probe_total + crop_product_time

row = {
    "scene": scene,
    "device": device or "default",
    "disable_adaptive_sampling": disable_adaptive,
    "ref_samples": CONFIG["ref_samples"],
    "draft_samples": CONFIG["draft_samples"],
    "refine_samples": CONFIG["refine_samples"],
    "grid": grid,
    "refine_threshold": threshold,
    "max_refine_tiles": max_tiles,
    "width": width,
    "height": height,
    "ref_time_s": round(ref_time, 4),
    "draft_time_s": round(draft_time, 4),
    "crop_product_time_s": round(crop_product_time, 4),
    "orientation_probe_time_s": round(orientation_probe_total, 4),
    "wrong_orientation_probe_time_s": round(wrong_orientation_time, 4),
    "product_total_time_s": round(product_time, 4),
    "experiment_total_time_s": round(experiment_time, 4),
    "product_speedup_vs_ref": round(ref_time / product_time, 4) if product_time > 0 else None,
    "experiment_speedup_vs_ref": round(ref_time / experiment_time, 4) if experiment_time > 0 else None,
    "draft": {k: v for k, v in draft_summary.items() if k != "tiles"},
    "final": {k: v for k, v in final_summary.items() if k != "tiles"},
    "failed_tile_count": len(failed),
    "selected_tile_count": len(selected),
    "refined_tile_fraction": round(len(selected) / float(grid * grid), 6),
    "crop_mode": "cx_batch_crop_manifest",
    "chosen_invert_y": chosen_invert_y,
    "orientation_probes": [
        {
            "invert_y": bool(p.get("invert_y")),
            "time_s": round(float(p["time_s"]), 4),
            "crop_vs_ref_tile_ssim": p.get("crop_vs_ref_tile_ssim"),
        }
        for p in orientation_probes
    ],
    "selected_tiles": [
        {
            "xi": t["xi"],
            "yi": t["yi"],
            "x0": t["x0"],
            "y0": t["y0"],
            "x1": t["x1"],
            "y1": t["y1"],
            "draft_tile_ssim": round(float(t["ssim"]), 9),
            "cx_crop": cx_crop_for((t["x0"], t["y0"], t["x1"], t["y1"])),
        }
        for t in selected
    ],
    "crop_results": [
        {
            "cx_crop": c.get("cx_crop"),
            "time_s": round(float(c["time_s"]), 4),
            "batch_time_s": round(float(c.get("batch_time_s", 0.0)), 4),
            "crop_vs_ref_tile_ssim": c.get("crop_vs_ref_tile_ssim"),
            "tile": {
                "xi": c["tile"]["xi"],
                "yi": c["tile"]["yi"],
            },
        }
        for c in crop_results
    ],
    "artifacts": {
        "ref_png": str(ref_png),
        "draft_png": str(draft_png),
        "merged_png": str(merged_png),
    },
}
print("CX_TILE_REFINE_ROW=" + json.dumps(row, sort_keys=True))
print("CX_TILE_REFINEMENT_OK=1")
""" % payload


def tile_refinement_cmd(
    root: str,
    scene: str,
    ref_samples: int,
    draft_samples: int,
    refine_samples: int,
    device: str,
    disable_adaptive_sampling: bool,
    grid: int,
    refine_threshold: float,
    max_refine_tiles: int,
) -> str:
    config = {
        "root": root,
        "binary": cycles_fork.binary_path(root),
        "scene": scene,
        "scene_safe": safe_name(scene.replace(".xml", "")),
        "ref_samples": ref_samples,
        "draft_samples": draft_samples,
        "refine_samples": refine_samples,
        "device": device,
        "disable_adaptive_sampling": disable_adaptive_sampling,
        "grid": grid,
        "refine_threshold": refine_threshold,
        "max_refine_tiles": max_refine_tiles,
        "width": 1024,
        "height": 512,
    }
    remote_py = build_remote_script(config)
    return "".join(
        [
            "set -e -o pipefail; export DEBIAN_FRONTEND=noninteractive; ",
            "apt-get update >/dev/null 2>&1; ",
            "apt-get install -y python3-numpy python3-pil file >/dev/null 2>&1; ",
            "cd " + cycles_fork.shell(root) + "; ",
            "export PYTHONPATH=/usr/lib/python3/dist-packages:"
            "/usr/local/lib/python3.12/dist-packages:"
            "/usr/local/lib/python3.11/dist-packages:${PYTHONPATH:-}; ",
            "python3 - <<'PY'\n",
            remote_py,
            "PY\n",
        ]
    )


def run_stage(pod: dict, name: str, cmd: str, timeout_s: int) -> dict:
    log(f"STAGE {name}: start (timeout {timeout_s}s)")
    t0 = time.time()
    try:
        rc, out, err = runpod.ssh(pod, cmd, timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001
        rec = {
            "stage": name,
            "ok": False,
            "elapsed_s": round(time.time() - t0, 1),
            "out_tail": "",
            "err_tail": f"{type(exc).__name__}: {exc}"[-2400:],
        }
        append_ledger({"event": "stage", **rec})
        return rec
    rec = {
        "stage": name,
        "ok": rc == 0,
        "elapsed_s": round(time.time() - t0, 1),
        "out_tail": (out or "")[-40000:],
        "err_tail": (err or "")[-2400:],
    }
    append_ledger({"event": "stage", **rec})
    log(f"STAGE {name}: rc={rc} elapsed={rec['elapsed_s']}s ok={rec['ok']}")
    if rec["out_tail"].strip():
        log(f"{name} stdout tail:\n{rec['out_tail']}")
    if not rec["ok"] and rec["err_tail"].strip():
        log(f"{name} stderr tail:\n{rec['err_tail']}")
    return rec


def parse_tile_rows(stage: dict) -> list[dict]:
    rows = []
    for match in re.finditer(r"^CX_TILE_REFINE_ROW=(\{.*\})$", stage.get("out_tail", ""), re.M):
        rows.append(json.loads(match.group(1)))
    return rows


def write_report(latest_rows: list[dict], result: dict | None) -> None:
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    lines = [
        f"# Tile Refinement - {DATE}",
        "",
        "## Summary",
        "",
        f"- Ledger: `{os.path.relpath(LEDGER, REPO)}`",
        f"- Latest run ok: `{bool(result and result.get('ok'))}`",
        f"- Rows this run: `{len(latest_rows)}`",
        "",
    ]
    if latest_rows:
        best = max(latest_rows, key=lambda r: r.get("product_speedup_vs_ref") or 0)
        lines.extend([
            "## Best Latest Row",
            "",
            "```json",
            json.dumps(best, indent=2, sort_keys=True),
            "```",
            "",
        ])
        lines.extend([
            "## Latest Rows",
            "",
            "| Scene | Draft -> refine | Selected tiles | Draft tier | Final tier | Final worst | Product speedup | Experiment speedup |",
            "|---|---:|---:|---|---|---:|---:|---:|",
        ])
        for row in latest_rows:
            lines.append(
                "| {scene} | {draft}->{refine} | {tiles}/{total} | {dtier} | {ftier} | "
                "{worst} | {ps} | {es} |".format(
                    scene=row.get("scene"),
                    draft=row.get("draft_samples"),
                    refine=row.get("refine_samples"),
                    tiles=row.get("selected_tile_count"),
                    total=row.get("grid", 0) * row.get("grid", 0),
                    dtier=(row.get("draft") or {}).get("tier"),
                    ftier=(row.get("final") or {}).get("tier"),
                    worst=(row.get("final") or {}).get("worst_tile_ssim"),
                    ps=row.get("product_speedup_vs_ref"),
                    es=row.get("experiment_speedup_vs_ref"),
                )
            )
        lines.append("")
    if result:
        lines.extend([
            "## Run Result",
            "",
            "```json",
            json.dumps(result, indent=2, sort_keys=True),
            "```",
            "",
        ])
    lines.extend([
        "## Interpretation",
        "",
        "- This is a real crop-render-and-merge branch, not a modeled tile-refinement row.",
        "- Crop renders use CX batch-manifest crop rows when the runtime root supports them.",
        "- Product and experiment speedups are equal unless an explicit diagnostic probe is added.",
        "- If final tier does not improve or product speedup loses to the best full-frame policy, cut this branch or move it deeper into a resident/warm-worker implementation.",
        "",
    ])
    with open(REPORT, "w") as f:
        f.write("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--remote-root", default=cycles_fork.DEFAULT_REMOTE_ROOT)
    parser.add_argument("--gpu-tier", choices=("hopper", "ada"), default="ada")
    parser.add_argument("--prebuilt-root-tar", default="")
    parser.add_argument("--scene", default="scene_cube_volume.xml")
    parser.add_argument("--include-synthetic-scene", action="store_true")
    parser.add_argument("--synthetic-name", default="cx_many_glass.xml")
    parser.add_argument("--ref-samples", type=int, default=4096)
    parser.add_argument("--draft-samples", type=int, default=16)
    parser.add_argument("--refine-samples", type=int, default=32)
    parser.add_argument("--device", default="CUDA")
    parser.add_argument("--allow-adaptive-sampling", action="store_true")
    parser.add_argument("--grid", type=int, default=8)
    parser.add_argument("--refine-threshold", type=float, default=0.95)
    parser.add_argument("--max-refine-tiles", type=int, default=4)
    parser.add_argument("--min-balance", type=float, default=4.0)
    parser.add_argument("--max-minutes", type=int, default=75)
    parser.add_argument("--stage-timeout-s", type=int, default=2400)
    parser.add_argument("--upload-timeout-s", type=int, default=900)
    parser.add_argument("--transfer-preflight-mb", type=int, default=4)
    parser.add_argument("--min-transfer-mbps", type=float, default=0.5)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.grid <= 0:
        raise SystemExit("--grid must be positive")
    if args.max_refine_tiles <= 0:
        raise SystemExit("--max-refine-tiles must be positive")
    if args.ref_samples <= max(args.draft_samples, args.refine_samples):
        raise SystemExit("--ref-samples must exceed draft/refine samples")

    tar_path = args.prebuilt_root_tar or default_tar_for_tier(args.gpu_tier)
    if not os.path.isfile(tar_path):
        raise SystemExit(f"prebuilt runtime tar not found: {tar_path}")
    scenes = parse_csv(args.scene)
    if args.include_synthetic_scene:
        scenes.append(args.synthetic_name)
    if len(scenes) != 1:
        raise SystemExit("tile refinement runner currently accepts exactly one scene")

    manifest = {
        "root": args.remote_root,
        "gpu_tier": args.gpu_tier,
        "prebuilt_root_tar": tar_path,
        "scene": scenes[0],
        "include_synthetic_scene": args.include_synthetic_scene,
        "synthetic_name": args.synthetic_name,
        "ref_samples": args.ref_samples,
        "draft_samples": args.draft_samples,
        "refine_samples": args.refine_samples,
        "device": args.device or "default",
        "disable_adaptive_sampling": not args.allow_adaptive_sampling,
        "grid": args.grid,
        "refine_threshold": args.refine_threshold,
        "max_refine_tiles": args.max_refine_tiles,
        "min_balance": args.min_balance,
        "max_minutes": args.max_minutes,
        "upload_timeout_s": args.upload_timeout_s,
        "transfer_preflight_mb": args.transfer_preflight_mb,
        "min_transfer_mbps": args.min_transfer_mbps,
    }
    if args.dry_run:
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return

    runpod.register_cleanup()
    bal0 = runpod.balance()["clientBalance"]
    log(f"balance ${bal0:.2f}; floor ${args.min_balance:.2f}")
    if bal0 <= args.min_balance:
        result = {"ok": False, "error": "balance_floor", "balance": bal0, "manifest": manifest}
        append_ledger({"event": "result", "result": result})
        print(json.dumps(result), flush=True)
        return

    pod = None
    records: list[dict] = []
    rows: list[dict] = []
    result = None
    try:
        pod = runpod.provision_reachable(
            gpu_plan_for_tier(args.gpu_tier),
            POD_IMAGE,
            disk_gb=POD_DISK_GB,
            require_cuda=True,
            name="cx-cycles-tile-refine",
        )
        append_ledger({"event": "pod_up", "pod": pod, "manifest": manifest})
        log(f"pod {pod['gpu']} {pod['id']} @ {pod['ip']}:{pod['port']}")
        runpod.arm_remote_watchdog(pod, min(WATCHDOG_TTL_S, args.max_minutes * 60 + 900))

        preflight = transfer_preflight(
            pod,
            args.transfer_preflight_mb,
            args.min_transfer_mbps,
            min(args.upload_timeout_s, 120),
        )
        records.append(preflight)
        if not preflight["ok"]:
            raise RuntimeError("transfer preflight failed; refusing full runtime tar upload")

        for rec in upload_prebuilt_root(
            pod,
            tar_path,
            args.remote_root,
            args.upload_timeout_s,
        ):
            records.append(rec)
            if not rec["ok"]:
                raise RuntimeError(f"prebuilt-root stage {rec['stage']} failed")

        smoke = run_stage(
            pod,
            "binary_smoke",
            cycles_fork.binary_smoke_stage(args.remote_root).cmd,
            900,
        )
        records.append(smoke)
        if not smoke["ok"]:
            raise RuntimeError("binary smoke failed")

        patch_smoke = run_stage(
            pod,
            "patch_cli_smoke",
            cycles_fork.patch_cli_smoke_stage(args.remote_root).cmd,
            1200,
        )
        records.append(patch_smoke)
        if not patch_smoke["ok"]:
            raise RuntimeError("patch CLI smoke failed; runtime root lacks --cx-crop support")

        if args.include_synthetic_scene:
            rec = run_stage(
                pod,
                "create_synthetic_scene",
                synthetic_scene_cmd(args.remote_root, args.synthetic_name),
                120,
            )
            records.append(rec)
            if not rec["ok"]:
                raise RuntimeError("synthetic scene creation failed")

        stage = run_stage(
            pod,
            f"tile_refinement_{safe_name(scenes[0])}",
            tile_refinement_cmd(
                args.remote_root,
                scenes[0],
                args.ref_samples,
                args.draft_samples,
                args.refine_samples,
                args.device,
                not args.allow_adaptive_sampling,
                args.grid,
                args.refine_threshold,
                args.max_refine_tiles,
            ),
            args.stage_timeout_s,
        )
        records.append(stage)
        rows = parse_tile_rows(stage)
        append_ledger({"event": "tile_refinement_rows", "scene": scenes[0], "rows": rows})
        if not stage["ok"] or "CX_TILE_REFINEMENT_OK" not in stage.get("out_tail", ""):
            raise RuntimeError("tile refinement stage failed")

        result = {
            "ok": bool(rows),
            "pod_gpu": pod["gpu"],
            "pod_cloud": pod["cloud"],
            "manifest": manifest,
            "rows": rows,
            "stages": [{k: r[k] for k in ("stage", "ok", "elapsed_s")} for r in records],
        }
    except Exception as exc:  # noqa: BLE001
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "manifest": manifest,
            "rows": rows,
            "stages": [{k: r[k] for k in ("stage", "ok", "elapsed_s")} for r in records],
        }
        log(f"ERROR {result['error']}")
    finally:
        if pod:
            log("tearing down")
            try:
                runpod.terminate(pod["id"])
            except Exception as exc:  # noqa: BLE001
                log(f"terminate error: {exc}")
        append_ledger({"event": "result", "result": result})
        try:
            b2 = runpod.balance()["clientBalance"]
        except Exception as exc:  # noqa: BLE001
            append_ledger({
                "event": "pod_down",
                "balance_after": None,
                "balance_error": f"{type(exc).__name__}: {exc}",
            })
            log(f"pod down; balance check failed: {type(exc).__name__}: {exc}")
        else:
            append_ledger({"event": "pod_down", "balance_after": b2})
            log(f"pod down. balance ${b2:.2f} (spent ${bal0 - b2:.2f})")
        write_report(rows, result)
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
