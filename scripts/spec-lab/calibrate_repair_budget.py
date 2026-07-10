#!/usr/bin/env python3
"""calibrate_repair_budget.py — L1 TUNED REPAIR BUDGET, calibrated OFFLINE ($0).

GENERALIZATION_PLAN_2026-07-10.md objective 4/L1. The strict-delivery GROW capstone
(classroom 4K, 2026-07-10) over-repaired: 32 tiles re-rendered, only 8 were below
the 0.95 worst-tile gate. Its banked receipt carries, PER REPAIRED TILE, the
reference-free aov_edge selector score ("divergence"), the true pre-repair SSIM
vs the reference ("ssim_pre") and the post-repair SSIM ("ssim_after") — plus the
measured per-frame repair render/composite wall-clock. That is everything needed
to calibrate, offline, a selection policy that would have caught every sub-gate
tile with minimal over-repair, and to PROJECT (MODELED, from measured per-tile
costs) the speedup the same run would have delivered.

Honesty rules encoded here:
  * A receipt's labeled tile set is COMPLETE for threshold derivation only when
    its POST-repair per-frame worst tile clears the gate on every frame: repaired
    tiles go to ~1.0, unrepaired tiles are unchanged, so post-worst >= gate proves
    every UNSELECTED tile was already >= gate — i.e. the selected set contains ALL
    sub-gate tiles and the labels have no blind spot. Receipts that fail this test
    (e.g. the top-12 aov_edge runs whose budget was too small) are used ONLY as
    selector-score stability cross-checks, never for threshold derivation.
  * Selection semantics match pod/exp_render_stack.py::select_repair_tiles exactly:
    a tile is selected iff score > floor (strict), ranked by (-score, frame, gy, gx),
    within the global top_k and per-frame caps.
  * Every projected speedup is labeled MODELED; the inputs (per-tile repair cost,
    T_ref, T_stack, selection cost) are MEASURED from the receipt itself. Two cost
    models bracket reality: "linear" (repair render AND composite scale per tile —
    optimistic) and "conservative" (render scales per tile; the composite pass is
    charged in FULL for every frame that still has >=1 repair).

Reads (never writes) the banked ledgers; prints the calibration table and a final
machine-readable JSON line. Default input is the capstone ledger; point --ledger at
docs/speed-lane-reports/spec-lab/scene_sweep_ledger.jsonl after the sweep runs to
validate the threshold cross-scene from the same receipt shape.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
DEFAULT_LEDGERS = [
    REPO / "docs/speed-lane-reports/spec-lab/integrated_spec_render_token_ledger.jsonl",
    REPO / "docs/speed-lane-reports/spec-lab/scene_sweep_ledger.jsonl",
]
RECEIPT_EVENTS = ("same_gpu_integrated_production_receipt", "scene_sweep_receipt")


# --------------------------------------------------------------------------- #
# Extraction                                                                    #
# --------------------------------------------------------------------------- #
def load_repair_receipts(paths: list[Path], selector: str) -> list[dict[str, Any]]:
    """All ledger receipts with a repaired-tile table for the requested selector."""
    out = []
    for path in paths:
        if not path.is_file():
            continue
        for line_no, line in enumerate(path.read_text().splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("event") not in RECEIPT_EVENTS:
                continue
            receipt = record.get("receipt") or {}
            rm = receipt.get("render_metrics") or {}
            if not rm.get("repair_enabled"):
                continue
            if str(rm.get("repair_selector", "two_draft")) != selector:
                continue
            if not rm.get("repaired_tile_ssim_after"):
                continue
            out.append(
                {
                    "source": f"{path.name}:{line_no}",
                    "ts": record.get("ts"),
                    "scene": rm.get("scene"),
                    "resolution": rm.get("resolution"),
                    "gpu": (receipt.get("gpu") or {}).get("gpu"),
                    "rm": rm,
                }
            )
    return out


def extract_tiles(rm: dict[str, Any]) -> list[dict[str, Any]]:
    """Per repaired tile: frame, (gy,gx), selector score, measured pre/post SSIM."""
    tiles = []
    for t in rm["repaired_tile_ssim_after"]:
        tiles.append(
            {
                "frame": int(t["frame"]),
                "tile": tuple(int(v) for v in t["tile"]),
                "score": float(t["divergence"]),
                "ssim_pre": float(t["ssim_pre"]),
                "ssim_after": float(t["ssim_after"]),
            }
        )
    return tiles


def coverage_complete(rm: dict[str, Any], gate: float) -> bool:
    """True iff post-repair per-frame worst tile >= gate on EVERY frame, which
    proves every unselected tile was already >= gate (labels are complete)."""
    post = rm.get("per_frame_worst_tile_ssim") or []
    return bool(post) and all(float(v) >= gate for v in post)


# --------------------------------------------------------------------------- #
# Selection policies (mirroring select_repair_tiles semantics: score > floor)    #
# --------------------------------------------------------------------------- #
def select_by_floor(tiles: list[dict[str, Any]], floor: float,
                    top_k: int | None = None, max_per_frame: int | None = None
                    ) -> list[dict[str, Any]]:
    """Strict floor + optional global/per-frame caps, ranked like the runner."""
    ranked = sorted(tiles, key=lambda t: (-t["score"], t["frame"], t["tile"]))
    picked: list[dict[str, Any]] = []
    per_frame: dict[int, int] = {}
    for t in ranked:
        if t["score"] <= floor:
            continue
        if top_k is not None and len(picked) >= top_k:
            break
        if max_per_frame is not None and per_frame.get(t["frame"], 0) >= max_per_frame:
            continue
        picked.append(t)
        per_frame[t["frame"]] = per_frame.get(t["frame"], 0) + 1
    return picked


def policy_stats(selected: list[dict[str, Any]], tiles: list[dict[str, Any]],
                 gate: float) -> dict[str, Any]:
    needed = [t for t in tiles if t["ssim_pre"] < gate]
    sel_ids = {(t["frame"], t["tile"]) for t in selected}
    caught = [t for t in needed if (t["frame"], t["tile"]) in sel_ids]
    over = [t for t in selected if t["ssim_pre"] >= gate]
    return {
        "n_selected": len(selected),
        "n_needed": len(needed),
        "n_caught": len(caught),
        "recall": (len(caught) / len(needed)) if needed else 1.0,
        "n_over_repair": len(over),
        "frames_touched": len({t["frame"] for t in selected}),
    }


# --------------------------------------------------------------------------- #
# MODELED speedup projection from MEASURED per-tile costs                        #
# --------------------------------------------------------------------------- #
def measured_costs(rm: dict[str, Any]) -> dict[str, float]:
    n = int(rm["repaired_tile_count"])
    render_total = float(sum(rm["per_frame_repair_render_s"]))
    composite_total = float(sum(rm["per_frame_repair_composite_s"]))
    frames = int(rm["frames"])
    return {
        "T_ref_s": float(rm["T_ref_s"]),
        "T_stack_s": float(rm["T_stack_s"]),
        "repair_cost_s": float(rm["repair_cost_s"]),  # render + composite (measured)
        "selection_cost_s": float(rm["selection_cost_s"]),
        "per_tile_render_s": render_total / n,
        "per_tile_composite_s": composite_total / n,
        "per_frame_composite_s": composite_total / frames,
        "n_repaired": float(n),
    }


def project_speedup(costs: dict[str, float], n_selected: int, frames_touched: int
                    ) -> dict[str, float]:
    """T_stack with the repair render/composite re-scaled to n_selected tiles.

    Selection cost is UNCHANGED (aov_edge scores every tile regardless of how many
    are then repaired). Both bracket models are MODELED; inputs are MEASURED.
    """
    base = costs["T_stack_s"] - costs["repair_cost_s"]  # anchors + selection + misc
    linear = base + n_selected * (costs["per_tile_render_s"] + costs["per_tile_composite_s"])
    conservative = (
        base
        + n_selected * costs["per_tile_render_s"]
        + frames_touched * costs["per_frame_composite_s"]
    )
    return {
        "T_stack_linear_s": round(linear, 2),
        "T_stack_conservative_s": round(conservative, 2),
        "speedup_linear_x": round(costs["T_ref_s"] / linear, 4),
        "speedup_conservative_x": round(costs["T_ref_s"] / conservative, 4),
        "label": "MODELED (measured per-tile costs, re-scaled tile count)",
    }


# --------------------------------------------------------------------------- #
# Calibration                                                                   #
# --------------------------------------------------------------------------- #
def threshold_candidates(tiles: list[dict[str, Any]]) -> list[float]:
    """Midpoints between consecutive distinct scores (+ a floor of 0.0), so every
    achievable selection set is represented exactly once."""
    scores = sorted({t["score"] for t in tiles})
    cands = [0.0]
    for lo, hi in zip(scores, scores[1:]):
        cands.append(round((lo + hi) / 2.0, 6))
    return cands


def build_threshold_table(tiles: list[dict[str, Any]], costs: dict[str, float],
                          gate: float) -> list[dict[str, Any]]:
    rows = []
    for floor in threshold_candidates(tiles):
        selected = select_by_floor(tiles, floor)
        stats = policy_stats(selected, tiles, gate)
        proj = project_speedup(costs, stats["n_selected"], stats["frames_touched"])
        rows.append({"floor": floor, **stats, **proj})
    return rows


def build_rank_table(tiles: list[dict[str, Any]], costs: dict[str, float],
                     gate: float, frames: int) -> list[dict[str, Any]]:
    rows = []
    max_k = max((sum(1 for t in tiles if t["frame"] == f) for f in range(frames)), default=0)
    for k in range(1, max_k + 1):
        selected = select_by_floor(tiles, 0.0, top_k=k * frames, max_per_frame=k)
        stats = policy_stats(selected, tiles, gate)
        proj = project_speedup(costs, stats["n_selected"], stats["frames_touched"])
        rows.append({"per_frame_k": k, "top_k": k * frames, **stats, **proj})
    return rows


def recommend(tiles: list[dict[str, Any]], rank_rows: list[dict[str, Any]],
              costs: dict[str, float], gate: float) -> dict[str, Any]:
    needed_scores = sorted(t["score"] for t in tiles if t["ssim_pre"] < gate)
    if not needed_scores:
        return {"note": f"no tile below gate {gate}; nothing to calibrate"}
    min_needed = needed_scores[0]
    below = sorted({t["score"] for t in tiles if t["score"] < min_needed}, reverse=True)
    next_below = below[0] if below else 0.0
    # Strict floor semantics (score > floor): any floor in (next_below, min_needed)
    # keeps recall 1.0; the midpoint maximizes robustness INSIDE this dataset.
    floor = round((min_needed + next_below) / 2.0, 6)
    floor_sel = select_by_floor(tiles, floor)
    floor_stats = policy_stats(floor_sel, tiles, gate)
    floor_proj = project_speedup(costs, floor_stats["n_selected"], floor_stats["frames_touched"])
    # Smallest per-frame rank cap with recall 1.0 (fewer tiles than a plain floor
    # whenever the score field is frame-stationary but the needed depth is not).
    rank_pick = next((r for r in rank_rows if r["recall"] == 1.0), None)
    # Combined: floor AND cap together (the runner applies both).
    combined = None
    if rank_pick is not None:
        sel = select_by_floor(tiles, floor, top_k=rank_pick["top_k"],
                              max_per_frame=rank_pick["per_frame_k"])
        stats = policy_stats(sel, tiles, gate)
        proj = project_speedup(costs, stats["n_selected"], stats["frames_touched"])
        combined = {
            "repair_min_divergence": floor,
            "repair_max_per_frame": rank_pick["per_frame_k"],
            "repair_top_k": rank_pick["top_k"],
            **stats, **proj,
        }
    oracle_sel = [t for t in tiles if t["ssim_pre"] < gate]
    oracle_stats = policy_stats(oracle_sel, tiles, gate)
    oracle = project_speedup(costs, oracle_stats["n_selected"], oracle_stats["frames_touched"])
    return {
        "gate": gate,
        "recommended_repair_min_divergence": floor,
        "floor_margin_to_min_needed_score": round(min_needed - floor, 6),
        "floor_margin_to_next_unneeded_below": round(floor - next_below, 6),
        "floor_policy": {**floor_stats, **floor_proj},
        "rank_policy": rank_pick,
        "combined_policy": combined,
        "oracle_ceiling": {**oracle_stats, **oracle,
                           "note": "selects exactly the sub-gate tiles; requires the "
                                   "reference — NOT achievable reference-free; ceiling only"},
        "caveat": (
            "derived from the aov_edge score distribution of the calibration receipts "
            "only; score-scale transfer across scenes/resolutions is UNVALIDATED until "
            "the scene sweep banks per-scene tile tables (run the sweep with "
            "repair_min_divergence=0.0, then re-run this tool on scene_sweep_ledger.jsonl)"
        ),
    }


def stability_check(complete: list[dict[str, Any]], others: list[dict[str, Any]]
                    ) -> list[dict[str, Any]]:
    """Cross-run selector-score drift for tiles present in multiple receipts."""
    if not complete or not others:
        return []
    ref_scores = {
        (t["frame"], t["tile"]): t["score"] for r in complete for t in extract_tiles(r["rm"])
    }
    rows = []
    for r in others:
        deltas = [
            abs(t["score"] - ref_scores[(t["frame"], t["tile"])])
            for t in extract_tiles(r["rm"])
            if (t["frame"], t["tile"]) in ref_scores
        ]
        if deltas:
            rows.append(
                {
                    "source": r["source"],
                    "scene": r["scene"],
                    "shared_tiles": len(deltas),
                    "max_abs_score_delta": round(max(deltas), 6),
                    "mean_abs_score_delta": round(sum(deltas) / len(deltas), 6),
                }
            )
    return rows


def fmt_row(row: dict[str, Any], key: str) -> str:
    return (
        f"| {row[key]:>9} | {row['n_selected']:>4} | {row['n_caught']}/{row['n_needed']:<4}"
        f" | {row['recall']:.2f} | {row['n_over_repair']:>4} | "
        f"{row['speedup_linear_x']:>7.3f}x | {row['speedup_conservative_x']:>7.3f}x |"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ledger", action="append", default=None,
        help="ledger path(s) to read (repeatable); default: the capstone integrated "
             "ledger + the scene-sweep ledger when present",
    )
    parser.add_argument("--selector", default="aov_edge",
                        help="repair selector whose scores are calibrated (default aov_edge; "
                             "two_draft scores are a different, measured-dead signal)")
    parser.add_argument("--gate", type=float, default=0.95,
                        help="strict-delivery worst-tile gate (default 0.95 — NEVER weaken)")
    args = parser.parse_args()

    paths = [Path(p) for p in args.ledger] if args.ledger else DEFAULT_LEDGERS
    receipts = load_repair_receipts(paths, args.selector)
    if not receipts:
        print(json.dumps({"error": f"no {args.selector} repair receipts found in "
                                   f"{[str(p) for p in paths]}"}))
        sys.exit(1)

    complete = [r for r in receipts if coverage_complete(r["rm"], args.gate)]
    partial = [r for r in receipts if not coverage_complete(r["rm"], args.gate)]
    print(f"# L1 repair-budget calibration ({args.selector}, gate {args.gate})")
    print(f"receipts found: {len(receipts)} "
          f"(complete-coverage: {len(complete)}, partial [stability-only]: {len(partial)})")
    for r in receipts:
        tag = "COMPLETE" if r in complete else "partial"
        print(f"  - {r['source']} ts={r['ts']} scene={r['scene']} {r['resolution']} "
              f"gpu={r['gpu']} [{tag}]")
    if not complete:
        print(json.dumps({"error": "no receipt has complete label coverage (post-repair "
                                   "worst tile >= gate on every frame); cannot derive a "
                                   "threshold without a labeling blind spot"}))
        sys.exit(1)

    summary_all = []
    for r in complete:
        rm = r["rm"]
        tiles = extract_tiles(rm)
        costs = measured_costs(rm)
        frames = int(rm["frames"])
        threshold_rows = build_threshold_table(tiles, costs, args.gate)
        rank_rows = build_rank_table(tiles, costs, args.gate, frames)
        rec = recommend(tiles, rank_rows, costs, args.gate)

        print(f"\n## receipt {r['source']} — scene={r['scene']} {r['resolution']} "
              f"({r['gpu']})")
        print(f"MEASURED: T_ref {costs['T_ref_s']:.1f}s, T_stack {costs['T_stack_s']:.1f}s "
              f"(measured speedup {costs['T_ref_s']/costs['T_stack_s']:.3f}x), "
              f"{int(costs['n_repaired'])} tiles repaired, per-tile render "
              f"{costs['per_tile_render_s']:.2f}s, per-tile composite "
              f"{costs['per_tile_composite_s']:.2f}s, selection {costs['selection_cost_s']:.1f}s")
        print("\n### score-floor policy (repair_min_divergence) — projections MODELED")
        print("|     floor | tile | caught | recall | over |  linear x |  conserv x |")
        print("|-----------|------|--------|--------|------|-----------|------------|")
        for row in threshold_rows:
            print(fmt_row(row, "floor"))
        print("\n### per-frame rank-cap policy (repair_max_per_frame) — projections MODELED")
        print("|  K/frame  | tile | caught | recall | over |  linear x |  conserv x |")
        print("|-----------|------|--------|--------|------|-----------|------------|")
        for row in rank_rows:
            print(fmt_row(row, "per_frame_k"))
        print("\n### recommendation")
        print(json.dumps(rec, indent=2, sort_keys=True))
        summary_all.append({"source": r["source"], "scene": r["scene"],
                            "resolution": r["resolution"], "recommendation": rec})

    drift = stability_check(complete, partial)
    if drift:
        print("\n### selector-score stability across runs (same scene/config)")
        print(json.dumps(drift, indent=2, sort_keys=True))

    print("\n" + json.dumps({"calibration": summary_all, "stability": drift}, sort_keys=True))


if __name__ == "__main__":
    main()
