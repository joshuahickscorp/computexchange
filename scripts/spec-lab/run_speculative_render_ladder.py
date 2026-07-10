#!/usr/bin/env python3
"""Quality-gated speculative render ladder over measured Cycles receipts.

Pixel/render speculation is not lossless speculative decoding. This runner
implements the honest protocol:

    draft -> verify -> gate -> refine/escalate -> receipt

When no live GPU is available it imports existing measured quality-ladder rows
and labels them as imported measurements, not new measurements.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path


HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent

DATE = "2026-07-09"
DEFAULT_SOURCE_LEDGER = REPO / "docs/speed-lane-reports/spec-lab/cycles_quality_ladder_ledger.jsonl"
DEFAULT_TILE_LEDGER = REPO / "docs/speed-lane-reports/spec-lab/tile_refinement_ledger.jsonl"
LEDGER = REPO / "docs/speed-lane-reports/spec-lab/speculative_render_ladder_ledger.jsonl"
REPORT = REPO / f"docs/speed-lane-reports/spec-lab/SPECULATIVE_RENDER_LADDER_{DATE}.md"
MAIN_REPORT = REPO / f"docs/speed-lane-reports/spec-lab/CX_CYCLES_KILLER_OVERNIGHT_{DATE}.md"

DELIVERY_GLOBAL = 0.98
DELIVERY_WORST_TILE = 0.95
PREVIEW_GLOBAL = 0.90
PREVIEW_WORST_TILE = 0.85


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def append_ledger(record: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as f:
        f.write(json.dumps({"ts": now(), **record}, sort_keys=True) + "\n")


def load_quality_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.is_file():
        return rows
    for line_no, line in enumerate(path.read_text().splitlines(), 1):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("event") == "quality_rows":
            for row in rec.get("rows", []):
                enriched = dict(row)
                enriched["_source_ledger"] = str(path)
                enriched["_source_line"] = line_no
                enriched["_evidence_type"] = "imported_measured"
                rows.append(enriched)
        result = rec.get("result")
        if rec.get("event") == "result" and isinstance(result, dict):
            for row in result.get("rows", []):
                enriched = dict(row)
                enriched["_source_ledger"] = str(path)
                enriched["_source_line"] = line_no
                enriched["_evidence_type"] = "imported_measured"
                rows.append(enriched)
    return dedupe_rows(rows)


def load_tile_refinement_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.is_file():
        return rows
    for line_no, line in enumerate(path.read_text().splitlines(), 1):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("event") == "tile_refinement_rows":
            for row in rec.get("rows", []):
                enriched = dict(row)
                enriched["variant"] = "tile_refine"
                enriched["_source_ledger"] = str(path)
                enriched["_source_line"] = line_no
                enriched["_evidence_type"] = "measured_tile_refinement"
                rows.append(enriched)
        result = rec.get("result")
        if rec.get("event") == "result" and isinstance(result, dict):
            for row in result.get("rows", []):
                enriched = dict(row)
                enriched["variant"] = "tile_refine"
                enriched["_source_ledger"] = str(path)
                enriched["_source_line"] = line_no
                enriched["_evidence_type"] = "measured_tile_refinement"
                rows.append(enriched)
    return dedupe_tile_rows(rows)


def dedupe_rows(rows: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for row in rows:
        key = (
            row.get("scene"),
            row.get("variant", "raw"),
            row.get("ref_samples"),
            row.get("samples"),
            row.get("quality"),
            row.get("worst_tile_ssim"),
            row.get("speedup_vs_ref"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def dedupe_tile_rows(rows: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for row in rows:
        final = row.get("final") or {}
        key = (
            row.get("scene"),
            row.get("draft_samples"),
            row.get("refine_samples"),
            row.get("grid"),
            row.get("selected_tile_count"),
            final.get("quality"),
            final.get("worst_tile_ssim"),
            row.get("product_speedup_vs_ref"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def classify(row: dict) -> str:
    global_ssim = float(row.get("quality") or 0.0)
    worst = float(row.get("worst_tile_ssim") or 0.0)
    if global_ssim >= DELIVERY_GLOBAL and worst >= DELIVERY_WORST_TILE:
        return "delivery"
    if global_ssim >= PREVIEW_GLOBAL and worst >= PREVIEW_WORST_TILE:
        return "preview"
    return "fail"


def classify_scores(global_ssim: float, worst: float) -> str:
    if global_ssim >= DELIVERY_GLOBAL and worst >= DELIVERY_WORST_TILE:
        return "delivery"
    if global_ssim >= PREVIEW_GLOBAL and worst >= PREVIEW_WORST_TILE:
        return "preview"
    return "fail"


def gate_row(row: dict) -> dict:
    tier = classify(row)
    ref_time = float(row.get("ref_time_s") or 0.0)
    draft_time = float(row.get("draft_time_s") or row.get("total_time_s") or 0.0)
    denoise_time = float(row.get("denoise_time_s") or 0.0)
    total_time = float(row.get("total_time_s") or (draft_time + denoise_time))
    if total_time <= 0 and draft_time > 0:
        total_time = draft_time + denoise_time
    ship_speedup = ref_time / total_time if ref_time > 0 and total_time > 0 else None
    whole_frame_escalated_time = total_time + ref_time if ref_time > 0 else None
    whole_frame_escalated_speedup = (
        ref_time / whole_frame_escalated_time
        if ref_time > 0 and whole_frame_escalated_time and whole_frame_escalated_time > 0
        else None
    )
    tile_fail_fraction_model = model_tile_fail_fraction(row)
    if tier == "delivery":
        action = "ship_delivery"
        escalation = "none"
    elif tier == "preview":
        action = "ship_preview_or_escalate_for_delivery"
        escalation = "failed_or_low-confidence tiles require higher spp before delivery"
    else:
        action = "reject_or_refine"
        escalation = "whole frame fallback is required unless tile-local refinement is implemented"
    return {
        "protocol": "draft->verify->gate->refine/escalate",
        "scene": row.get("scene"),
        "variant": row.get("variant", "raw"),
        "device": row.get("device"),
        "evidence_type": row.get("_evidence_type", "unknown"),
        "source_ledger": row.get("_source_ledger"),
        "source_line": row.get("_source_line"),
        "ref_samples": row.get("ref_samples"),
        "draft_samples": row.get("samples"),
        "ref_time_s": ref_time,
        "draft_time_s": draft_time,
        "denoise_time_s": denoise_time,
        "total_time_s": total_time,
        "global_ssim": row.get("quality"),
        "worst_tile_ssim": row.get("worst_tile_ssim"),
        "p5_tile_ssim": row.get("p5_tile_ssim"),
        "png_mae": row.get("png_mae"),
        "png_maxe": row.get("png_maxe"),
        "tile_count": row.get("tile_count"),
        "tier": tier,
        "action": action,
        "escalation": escalation,
        "accepted_tile_fraction_model": (
            round(1.0 - tile_fail_fraction_model, 4)
            if tile_fail_fraction_model is not None
            else None
        ),
        "net_speedup_if_shipped_x": round(ship_speedup, 4) if ship_speedup else None,
        "whole_frame_escalated_speedup_x": (
            round(whole_frame_escalated_speedup, 4)
            if whole_frame_escalated_speedup
            else None
        ),
    }


def gate_tile_refinement_row(row: dict) -> dict:
    final = row.get("final") or {}
    draft = row.get("draft") or {}
    global_ssim = float(final.get("quality") or 0.0)
    worst = float(final.get("worst_tile_ssim") or 0.0)
    tier = classify_scores(global_ssim, worst)
    ref_time = float(row.get("ref_time_s") or 0.0)
    total_time = float(row.get("product_total_time_s") or row.get("experiment_total_time_s") or 0.0)
    speedup = (
        float(row.get("product_speedup_vs_ref"))
        if row.get("product_speedup_vs_ref") is not None
        else (ref_time / total_time if ref_time > 0 and total_time > 0 else None)
    )
    if tier == "delivery":
        action = "ship_delivery_after_tile_refinement"
        escalation = "actual crop-refined receipt cleared delivery gates"
    elif tier == "preview":
        action = "refine_more_or_fallback_for_delivery"
        escalation = "actual crop refinement improved quality but did not clear delivery worst-tile gate"
    else:
        action = "reject_or_fallback"
        escalation = "actual crop refinement failed quality gates"
    grid = int(row.get("grid") or 0)
    selected = int(row.get("selected_tile_count") or 0)
    tile_count = grid * grid if grid > 0 else final.get("tile_count")
    accepted_fraction = (
        round(1.0 - (selected / float(tile_count)), 4)
        if tile_count else None
    )
    return {
        "protocol": "draft->verify->actual-crop-refine->merge->gate",
        "scene": row.get("scene"),
        "variant": "tile_refine",
        "device": row.get("device"),
        "evidence_type": row.get("_evidence_type", "measured_tile_refinement"),
        "source_ledger": row.get("_source_ledger"),
        "source_line": row.get("_source_line"),
        "ref_samples": row.get("ref_samples"),
        "draft_samples": row.get("draft_samples"),
        "refine_samples": row.get("refine_samples"),
        "ref_time_s": ref_time,
        "draft_time_s": float(row.get("draft_time_s") or 0.0),
        "denoise_time_s": 0.0,
        "crop_product_time_s": row.get("crop_product_time_s"),
        "total_time_s": total_time,
        "global_ssim": final.get("quality"),
        "worst_tile_ssim": final.get("worst_tile_ssim"),
        "p5_tile_ssim": final.get("p5_tile_ssim"),
        "draft_worst_tile_ssim": draft.get("worst_tile_ssim"),
        "png_mae": final.get("png_mae"),
        "png_maxe": final.get("png_maxe"),
        "tile_count": final.get("tile_count"),
        "tier": tier,
        "action": action,
        "escalation": escalation,
        "accepted_tile_fraction_actual": accepted_fraction,
        "refined_tile_fraction": row.get("refined_tile_fraction"),
        "selected_tile_count": selected,
        "failed_tile_count": row.get("failed_tile_count"),
        "crop_mode": row.get("crop_mode"),
        "net_speedup_if_shipped_x": round(speedup, 4) if speedup else None,
        "whole_frame_escalated_speedup_x": (
            round(ref_time / (total_time + ref_time), 4)
            if ref_time > 0 and total_time > 0
            else None
        ),
    }


def model_tile_fail_fraction(row: dict) -> float | None:
    tile_count = row.get("tile_count")
    worst = row.get("worst_tile_ssim")
    p5 = row.get("p5_tile_ssim")
    if not tile_count or worst is None or p5 is None:
        return None
    threshold = DELIVERY_WORST_TILE
    if float(worst) >= threshold:
        return 0.0
    if float(p5) < threshold:
        return 0.05
    return round(1.0 / max(int(tile_count), 1), 4)


def filter_rows(rows: list[dict], scene_pattern: str, include_variants: str) -> list[dict]:
    if scene_pattern:
        rx = re.compile(scene_pattern)
        rows = [r for r in rows if rx.search(str(r.get("scene", "")))]
    variants = {v.strip() for v in include_variants.split(",") if v.strip()}
    if variants:
        rows = [r for r in rows if str(r.get("variant", "raw")) in variants]
    return rows


def summarize(gates: list[dict]) -> dict:
    if not gates:
        return {"gate_count": 0}
    delivery = [g for g in gates if g["tier"] == "delivery"]
    preview = [g for g in gates if g["tier"] in {"preview", "delivery"}]
    fail = [g for g in gates if g["tier"] == "fail"]
    best_delivery = max(delivery, key=lambda g: g.get("net_speedup_if_shipped_x") or 0, default=None)
    best_preview = max(preview, key=lambda g: g.get("net_speedup_if_shipped_x") or 0, default=None)
    return {
        "gate_count": len(gates),
        "delivery_count": len(delivery),
        "preview_or_better_count": len(preview),
        "fail_count": len(fail),
        "scenes": sorted({str(g["scene"]) for g in gates}),
        "actual_tile_refinement_count": len([
            g for g in gates if g.get("evidence_type") == "measured_tile_refinement"
        ]),
        "best_delivery": best_delivery,
        "best_preview": best_preview,
        "worst_failures": sorted(
            fail,
            key=lambda g: float(g.get("worst_tile_ssim") or 0.0),
        )[:5],
    }


def write_report(gates: list[dict], summary: dict, source: Path, tile_source: Path) -> None:
    has_oidn = any(gate.get("variant") == "oidn" for gate in gates)
    lines = [
        f"# Speculative Render Ladder - {DATE}",
        "",
        "## Summary",
        "",
        f"- Source ledger: `{source.relative_to(REPO) if source.is_relative_to(REPO) else source}`",
        f"- Tile refinement ledger: `{tile_source.relative_to(REPO) if tile_source.is_relative_to(REPO) else tile_source}`",
        f"- Gate rows: `{summary.get('gate_count', 0)}`",
        f"- Actual tile-refinement rows: `{summary.get('actual_tile_refinement_count', 0)}`",
        f"- Delivery rows: `{summary.get('delivery_count', 0)}`",
        f"- Preview-or-better rows: `{summary.get('preview_or_better_count', 0)}`",
        f"- Failed rows: `{summary.get('fail_count', 0)}`",
        f"- Output ledger: `{LEDGER.relative_to(REPO)}`",
        "",
        "## Policy",
        "",
        f"- Delivery: global SSIM >= `{DELIVERY_GLOBAL}` and worst-tile SSIM >= `{DELIVERY_WORST_TILE}`",
        f"- Preview: global SSIM >= `{PREVIEW_GLOBAL}` and worst-tile SSIM >= `{PREVIEW_WORST_TILE}`",
        "- Global SSIM alone cannot pass.",
        "",
        "## Best Delivery Gate",
        "",
        "```json",
        json.dumps(summary.get("best_delivery"), indent=2, sort_keys=True),
        "```",
        "",
        "## Best Preview Gate",
        "",
        "```json",
        json.dumps(summary.get("best_preview"), indent=2, sort_keys=True),
        "```",
        "",
        "## Ladder Rows",
        "",
        "| Scene | Variant | Draft spp | Tier | Global | Worst tile | Speedup if shipped | Action | Evidence |",
        "|---|---|---:|---|---:|---:|---:|---|---|",
    ]
    for gate in sorted(
        gates,
        key=lambda g: (
            str(g.get("scene")),
            str(g.get("variant")),
            int(g.get("draft_samples") or 0),
        ),
    ):
        lines.append(
            "| {scene} | {variant} | {samples} | {tier} | {global_ssim} | {worst} | {speedup} | {action} | {evidence} |".format(
                scene=gate.get("scene"),
                variant=gate.get("variant"),
                samples=gate.get("draft_samples"),
                tier=gate.get("tier"),
                global_ssim=gate.get("global_ssim"),
                worst=gate.get("worst_tile_ssim"),
                speedup=gate.get("net_speedup_if_shipped_x"),
                action=gate.get("action"),
                evidence=gate.get("evidence_type"),
            )
        )
    lines.extend([
        "",
        "## Interpretation",
        "",
        "- This is a speculative render protocol scaffold using imported measured Cycles quality rows and actual tile-refinement receipts when present.",
        "- Delivery rows are plausible immediate CX product levers: low-spp drafts verified against high-spp references.",
        "- Actual tile-refinement rows are scored as measured `tile_refine` variants, not modeled multipliers.",
        "- Failed rows identify the tile/refinement work that must exist before broader claims.",
        (
            "- OIDN rows are validated in the imported quality ledger and include denoise time."
            if has_oidn else
            "- OIDN is not present in these gate rows; denoise speedups are not claimed here."
        ),
    ])
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n")


def append_to_main_report(summary: dict) -> None:
    block = [
        "",
        "## Speculative Render Ladder",
        "",
        f"- Report: `{REPORT.relative_to(REPO)}`",
        f"- Ledger: `{LEDGER.relative_to(REPO)}`",
        f"- Gate rows: `{summary.get('gate_count', 0)}`",
        f"- Delivery rows: `{summary.get('delivery_count', 0)}`",
        f"- Best delivery speedup: `{(summary.get('best_delivery') or {}).get('net_speedup_if_shipped_x')}`",
        "",
        "Boundary: these are imported measured Cycles quality-ladder receipts unless a live run is explicitly added.",
        "",
        "## Next-Step Prompt",
        "",
        "```text",
        "/goal Continue `/Users/scammermike/Downloads/computexchange/docs/research/CYCLES_KILLER_SPECULATIVE_OVERNIGHT_GOAL_2026-07-09.md`: run a live CUDA quality/speculative ladder when RunPod credentials are present, validate OIDN with background remote logging, add a representative CX scene, preserve license attribution, keep cloud teardown receipts, and do not claim Cycles-killer status without measured global+worst-tile quality and wall-clock receipts.",
        "```",
    ]
    MAIN_REPORT.parent.mkdir(parents=True, exist_ok=True)
    if MAIN_REPORT.exists():
        MAIN_REPORT.write_text(MAIN_REPORT.read_text() + "\n".join(block) + "\n")
    else:
        MAIN_REPORT.write_text("# CX Cycles-Killer Overnight Report - 2026-07-09\n" + "\n".join(block) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-ledger", default=str(DEFAULT_SOURCE_LEDGER))
    parser.add_argument("--tile-ledger", default=str(DEFAULT_TILE_LEDGER))
    parser.add_argument("--scene-regex", default="")
    parser.add_argument("--variants", default="raw,oidn,tile_refine")
    parser.add_argument("--min-rows", type=int, default=1)
    args = parser.parse_args()

    source = Path(args.source_ledger)
    tile_source = Path(args.tile_ledger)
    append_ledger({
        "event": "start",
        "argv": sys.argv[1:],
        "source_ledger": str(source),
        "tile_ledger": str(tile_source),
    })
    rows = filter_rows(load_quality_rows(source), args.scene_regex, args.variants)
    tile_rows = filter_rows(load_tile_refinement_rows(tile_source), args.scene_regex, args.variants)
    gates = [gate_row(row) for row in rows]
    gates.extend(gate_tile_refinement_row(row) for row in tile_rows)
    summary = summarize(gates)
    append_ledger({"event": "gates", "summary": summary, "gates": gates})
    write_report(gates, summary, source, tile_source)
    append_to_main_report(summary)
    result = {
        "ok": len(gates) >= args.min_rows,
        "status": "imported_measured_receipts" if gates else "missing_quality_rows",
        "summary": summary,
        "reports": {
            "ledger": str(LEDGER),
            "report": str(REPORT),
            "main": str(MAIN_REPORT),
        },
    }
    append_ledger({"event": "result", "result": result})
    print(json.dumps(result, sort_keys=True), flush=True)
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
