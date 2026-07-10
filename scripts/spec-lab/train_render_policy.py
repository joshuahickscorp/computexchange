#!/usr/bin/env python3
"""Train a measured-receipt render routing policy from quality-ladder rows."""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path


HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
DATE = "2026-07-09"
SOURCE = REPO / "docs/speed-lane-reports/spec-lab/cycles_quality_ladder_ledger.jsonl"
TILE_SOURCE = REPO / "docs/speed-lane-reports/spec-lab/tile_refinement_ledger.jsonl"
LEDGER = REPO / "docs/speed-lane-reports/spec-lab/render_policy_training_ledger.jsonl"
POLICY_JSON = REPO / f"docs/speed-lane-reports/spec-lab/render_policy_{DATE}.json"
REPORT = REPO / f"docs/speed-lane-reports/spec-lab/RENDER_POLICY_TRAINING_{DATE}.md"


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def append_ledger(record: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as f:
        f.write(json.dumps({"ts": now(), **record}, sort_keys=True) + "\n")


def load_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    current_pod = {}
    if not path.is_file():
        return rows
    for line_no, line in enumerate(path.read_text().splitlines(), 1):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("event") == "pod_up":
            pod = rec.get("pod") or {}
            current_pod = {"pod_gpu": pod.get("gpu"), "pod_cloud": pod.get("cloud")}
        if rec.get("event") == "result" and isinstance(rec.get("result"), dict):
            result = rec["result"]
            current_pod = {
                "pod_gpu": result.get("pod_gpu") or current_pod.get("pod_gpu"),
                "pod_cloud": result.get("pod_cloud") or current_pod.get("pod_cloud"),
            }
            for row in result.get("rows", []):
                enriched = dict(row)
                enriched.update(current_pod)
                enriched["source_line"] = line_no
                rows.append(enriched)
        if rec.get("event") == "quality_rows":
            for row in rec.get("rows", []):
                enriched = dict(row)
                enriched.update(current_pod)
                enriched["source_line"] = line_no
                rows.append(enriched)
    return dedupe(rows)


def load_tile_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    current_pod = {}
    if not path.is_file():
        return rows
    for line_no, line in enumerate(path.read_text().splitlines(), 1):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("event") == "pod_up":
            pod = rec.get("pod") or {}
            current_pod = {"pod_gpu": pod.get("gpu"), "pod_cloud": pod.get("cloud")}
        if rec.get("event") == "result" and isinstance(rec.get("result"), dict):
            result = rec["result"]
            current_pod = {
                "pod_gpu": result.get("pod_gpu") or current_pod.get("pod_gpu"),
                "pod_cloud": result.get("pod_cloud") or current_pod.get("pod_cloud"),
            }
            for row in result.get("rows", []):
                rows.append(flatten_tile_row(row, current_pod, line_no))
        if rec.get("event") == "tile_refinement_rows":
            for row in rec.get("rows", []):
                rows.append(flatten_tile_row(row, current_pod, line_no))
    return dedupe(rows)


def flatten_tile_row(row: dict, pod: dict, source_line: int) -> dict:
    final = row.get("final") or {}
    out = {
        "scene": row.get("scene"),
        "variant": "tile_refine",
        "samples": row.get("draft_samples"),
        "refine_samples": row.get("refine_samples"),
        "ref_samples": row.get("ref_samples"),
        "speedup_vs_ref": row.get("product_speedup_vs_ref"),
        "quality": final.get("quality"),
        "worst_tile_ssim": final.get("worst_tile_ssim"),
        "p5_tile_ssim": final.get("p5_tile_ssim"),
        "draft_time_s": row.get("draft_time_s"),
        "crop_product_time_s": row.get("crop_product_time_s"),
        "total_time_s": row.get("product_total_time_s"),
        "ref_time_s": row.get("ref_time_s"),
        "crop_mode": row.get("crop_mode"),
        "selected_tile_count": row.get("selected_tile_count"),
        "failed_tile_count": row.get("failed_tile_count"),
        "refined_tile_fraction": row.get("refined_tile_fraction"),
        "source_line": source_line,
    }
    out.update(pod)
    return out


def dedupe(rows: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for row in rows:
        key = (
            row.get("scene"),
            row.get("variant", "raw"),
            row.get("samples"),
            row.get("ref_samples"),
            row.get("quality"),
            row.get("worst_tile_ssim"),
            row.get("speedup_vs_ref"),
            row.get("pod_gpu"),
            row.get("crop_mode"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def is_delivery(row: dict) -> bool:
    return (
        float(row.get("quality") or 0) >= 0.98
        and float(row.get("worst_tile_ssim") or 0) >= 0.95
    )


def next_action(best: dict | None, failures: list[dict]) -> str:
    if not best:
        return "no_delivery_policy: raise samples or implement tile refinement"
    speed = float(best.get("speedup_vs_ref") or 0)
    if speed >= 10:
        return "ship_10x_policy: validate on more representative scenes and warm worker"
    if failures:
        return "tile_refinement_or_trained_thresholds: low-spp rows are fast but fail worst-tile"
    return "warm_worker_or_scene_specialization: quality passes but speed ceiling is below 10x"


def train_policy(rows: list[dict]) -> dict:
    by_scene: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_scene[str(row.get("scene"))].append(row)

    policies = []
    for scene, scene_rows in sorted(by_scene.items()):
        delivery = [r for r in scene_rows if is_delivery(r)]
        failures = [r for r in scene_rows if not is_delivery(r)]
        best_delivery = max(delivery, key=lambda r: float(r.get("speedup_vs_ref") or 0), default=None)
        fastest_fail = max(failures, key=lambda r: float(r.get("speedup_vs_ref") or 0), default=None)
        policies.append({
            "scene": scene,
            "row_count": len(scene_rows),
            "delivery_count": len(delivery),
            "best_delivery": compact_row(best_delivery),
            "fastest_failing_draft": compact_row(fastest_fail),
            "learned_action": next_action(best_delivery, failures),
        })

    global_best = max(
        (p["best_delivery"] for p in policies if p["best_delivery"]),
        key=lambda r: float(r.get("speedup_vs_ref") or 0),
        default=None,
    )
    hard_best = max(
        (
            p["best_delivery"]
            for p in policies
            if p["best_delivery"] and p["scene"] != "scene_world_volume.xml"
        ),
        key=lambda r: float(r.get("speedup_vs_ref") or 0),
        default=None,
    )
    return {
        "source": str(SOURCE),
        "tile_source": str(TILE_SOURCE),
        "row_count": len(rows),
        "global_best_delivery": global_best,
        "hard_scene_best_delivery": hard_best,
        "policies": policies,
        "method": (
            "receipt-derived policy table: best delivery rows define ship policy; fastest failed "
            "rows define tile-refinement/training targets"
        ),
    }


def compact_row(row: dict | None) -> dict | None:
    if not row:
        return None
    keys = [
        "scene",
        "variant",
        "samples",
        "ref_samples",
        "speedup_vs_ref",
        "quality",
        "worst_tile_ssim",
        "p5_tile_ssim",
        "draft_time_s",
        "denoise_time_s",
        "total_time_s",
        "ref_time_s",
        "refine_samples",
        "crop_product_time_s",
        "crop_mode",
        "selected_tile_count",
        "failed_tile_count",
        "refined_tile_fraction",
        "pod_gpu",
        "pod_cloud",
        "source_line",
    ]
    return {k: row.get(k) for k in keys if k in row}


def write_outputs(policy: dict) -> None:
    POLICY_JSON.parent.mkdir(parents=True, exist_ok=True)
    POLICY_JSON.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n")

    lines = [
        f"# Render Policy Training - {DATE}",
        "",
        "## Summary",
        "",
        f"- Source ledger: `{SOURCE.relative_to(REPO)}`",
        f"- Tile refinement ledger: `{TILE_SOURCE.relative_to(REPO)}`",
        f"- Policy JSON: `{POLICY_JSON.relative_to(REPO)}`",
        f"- Rows trained on: `{policy['row_count']}`",
        f"- Global best delivery: `{(policy.get('global_best_delivery') or {}).get('speedup_vs_ref')}`",
        f"- Hard-scene best delivery: `{(policy.get('hard_scene_best_delivery') or {}).get('speedup_vs_ref')}`",
        "",
        "## Learned Policies",
        "",
        "| Scene | Best delivery | Fastest failed draft | Learned action |",
        "|---|---:|---:|---|",
    ]
    for item in policy["policies"]:
        best = item.get("best_delivery") or {}
        fail = item.get("fastest_failing_draft") or {}
        best_txt = (
            f"{best.get('speedup_vs_ref')}x @ {best.get('samples')} spp {best.get('variant')}"
            if best else "none"
        )
        fail_txt = (
            f"{fail.get('speedup_vs_ref')}x @ {fail.get('samples')} spp {fail.get('variant')}"
            if fail else "none"
        )
        lines.append(
            f"| `{item['scene']}` | {best_txt} | {fail_txt} | `{item['learned_action']}` |"
        )
    lines.extend([
        "",
        "## Interpretation",
        "",
        "- This is not a synthetic benchmark or a modeled multiplier.",
        "- It trains a routing policy from measured render receipts only.",
        "- Actual tile-refinement receipts are included as `tile_refine` rows when present.",
        "- Friendly scene classes can route to the `10x+` low-spp policy.",
        "- Hard scenes route to tile refinement, higher-spp anchors, or trained threshold tuning.",
        "- The next real implementation step is to replace the modeled tile-refinement action with",
        "  actual crop/tile rerender and merge receipts.",
    ])
    REPORT.write_text("\n".join(lines) + "\n")


def main() -> None:
    global SOURCE
    global TILE_SOURCE
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-ledger", default=str(SOURCE))
    parser.add_argument("--tile-ledger", default=str(TILE_SOURCE))
    args = parser.parse_args()

    SOURCE = Path(args.source_ledger)
    TILE_SOURCE = Path(args.tile_ledger)
    append_ledger({"event": "start", "source": str(SOURCE), "tile_source": str(TILE_SOURCE)})
    rows = load_rows(SOURCE) + load_tile_rows(TILE_SOURCE)
    policy = train_policy(rows)
    write_outputs(policy)
    result = {
        "ok": bool(rows),
        "policy_json": str(POLICY_JSON),
        "report": str(REPORT),
        "global_best_delivery": policy.get("global_best_delivery"),
        "hard_scene_best_delivery": policy.get("hard_scene_best_delivery"),
    }
    append_ledger({"event": "result", "result": result})
    print(json.dumps(result, sort_keys=True), flush=True)
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
