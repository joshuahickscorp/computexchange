#!/usr/bin/env python3
"""RunPod cost estimator/quote derived from MEASURED campaign ledgers.

Reads the spec-lab campaign ledgers (defensively — malformed lines are skipped,
never fatal):

  - integrated_spec_render_token_ledger.jsonl   (pipeline runs, 1080p + 4K [+repair])
  - cross_denoiser_probe_ledger.jsonl           (single-frame probe)
  - multi_selector_probe_ledger.jsonl           (single-frame probe)
  - reference_consistency_ledger.jsonl          (single-frame probe)

and derives:

  1. Per-run REAL $ spend from RunPod balance deltas. Every preflight row logs
     `preflight.balance.clientBalance`; the cost of a run is the balance at its
     own preflight minus the first balance snapshot (across ALL ledgers) at or
     after the run's terminal event. Label: MEASURED (balance-delta). Where no
     later snapshot exists the cost is MODELED (session hours x basis $/hr).
  2. Per-GPU effective $/hr, extracted as the median implied rate
     (measured cost / preflight->terminal session hours) over measured runs.
     Documented SECURE list-price constants (A100 1.42, H100 3.02) are kept as
     the fallback basis when fewer than MIN_RATE_SAMPLES measured samples exist.
  3. Failed-attempt overhead: provisioning prunes / SSH-killed renders /
     abandoned attempts, each with its own measured balance delta — reported as
     a contingency percentage, never silently folded into per-run quotes.
  4. A quote table for the sequenced cloud runs: 1080p pipeline/scene, 4K
     pipeline (no repair), 4K strict delivery w/ repair, single-frame probe,
     cross-arch CUDA half, and an N-scene 1080p sweep (warm-pod formula).

Honesty labels used throughout: MEASURED (a real balance delta or real wall
clock), MEASURED-derived (median/aggregate of MEASURED values), MODELED
(arithmetic on MEASURED components — e.g. rate x hours — not itself observed).
No number is ever a product of two lanes' multipliers; this tool only handles
$ and wall-clock.

Usage:
  python3 scripts/spec-lab/runpod_cost_quote.py                # markdown to stdout
  python3 scripts/spec-lab/runpod_cost_quote.py --json-out q.json --md-out q.md
  python3 scripts/spec-lab/runpod_cost_quote.py --ledger-dir docs/speed-lane-reports/spec-lab

Stdlib only. LOCAL ONLY: this tool never talks to the RunPod API — it reads
ledgers already on disk.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants (documented; every use is labeled at the point of use)
# ---------------------------------------------------------------------------

# Documented RunPod SECURE list prices ($/hr) — the fallback basis when the
# ledgers do not contain enough balance-delta samples to extract a median.
DOCUMENTED_RATES_USD_HR: Dict[str, float] = {
    "A100": 1.42,
    "H100": 3.02,
}

# Minimum measured samples before the extracted median replaces the
# documented constant as the quote basis.
MIN_RATE_SAMPLES = 2

# Sessions shorter than this are too noisy for implied-rate extraction
# (balance updates are not instantaneous).
MIN_SESSION_HOURS_FOR_RATE = 0.05

LEDGER_FILES = (
    "integrated_spec_render_token_ledger.jsonl",
    "cross_denoiser_probe_ledger.jsonl",
    "multi_selector_probe_ledger.jsonl",
    "reference_consistency_ledger.jsonl",
)

# Event-name suffixes that terminate a pending attempt.
TERMINAL_SUCCESS_MARKERS = ("receipt", "_result")
TERMINAL_PRUNE_MARKERS = ("_pruned",)


# ---------------------------------------------------------------------------
# Defensive parsing
# ---------------------------------------------------------------------------

def parse_jsonl(path: Path) -> List[Dict[str, Any]]:
    """Parse a JSONL file defensively: skip blank / malformed / non-object lines."""
    rows: List[Dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def parse_ts(value: Any) -> Optional[datetime]:
    """Parse ledger timestamps like '2026-07-09T14:03:40-0400'. None on failure."""
    if not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    try:  # tolerate ISO with colon in the offset
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _get_nested(d: Any, *keys: str) -> Any:
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def gpu_key(name: Any) -> Optional[str]:
    """Normalize 'NVIDIA A100 80GB PCIe' -> 'A100', etc."""
    if not isinstance(name, str) or not name.strip():
        return None
    up = name.upper()
    for key in ("A100", "H100", "H200", "B200", "B300", "L40S", "A6000"):
        if key in up:
            return key
    return name.strip().split()[0]


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_snapshots(rows_by_ledger: Dict[str, List[Dict[str, Any]]]) -> List[Tuple[datetime, float]]:
    """All (ts, clientBalance) points across every ledger, time-sorted.

    Only rows whose event ends with '_preflight' count: the balance is read at
    preflight time. Pruned rows EMBED a stale copy of their run's preflight
    block (same balance, later ts) — treating those as fresh readings would
    zero out the measured cost of every pruned attempt.
    """
    snaps: List[Tuple[datetime, float]] = []
    for rows in rows_by_ledger.values():
        for row in rows:
            event = row.get("event")
            if not (isinstance(event, str) and event.endswith("_preflight")):
                continue
            ts = parse_ts(row.get("ts"))
            bal = _get_nested(row, "preflight", "balance", "clientBalance")
            if ts is not None and isinstance(bal, (int, float)):
                snaps.append((ts, float(bal)))
    snaps.sort(key=lambda p: p[0])
    return snaps


def extract_attempts(rows: List[Dict[str, Any]], ledger_name: str) -> List[Dict[str, Any]]:
    """Scan one ledger in file order pairing each *_preflight with its terminal
    event (receipt / *_result / *_pruned). A preflight superseded by another
    preflight with no terminal event in between is outcome='abandoned' (its
    spend is real: provisioning that never reached a run)."""
    attempts: List[Dict[str, Any]] = []
    pending: Optional[Dict[str, Any]] = None

    def close(outcome: str, row: Optional[Dict[str, Any]], end_ts: Optional[datetime]) -> None:
        nonlocal pending
        if pending is None:
            return
        pending["outcome"] = outcome
        pending["end_ts"] = end_ts
        if row is not None:
            pod = row.get("pod") if isinstance(row.get("pod"), dict) else {}
            receipt = row.get("receipt") if isinstance(row.get("receipt"), dict) else {}
            rgpu = _get_nested(receipt, "gpu", "gpu") or pod.get("gpu")
            rcloud = _get_nested(receipt, "gpu", "cloud") or pod.get("cloud")
            pending["gpu"] = gpu_key(rgpu)
            pending["cloud"] = rcloud
            pending["receipt"] = receipt or None
            pending["result_metrics"] = row.get("metrics") if isinstance(row.get("metrics"), dict) else None
        attempts.append(pending)
        pending = None

    for row in rows:
        event = row.get("event")
        if not isinstance(event, str):
            continue
        ts = parse_ts(row.get("ts"))
        if event.endswith("_preflight"):
            if pending is not None:
                close("abandoned", None, ts)  # superseded, never reached a terminal event
            pending = {
                "ledger": ledger_name,
                "event_preflight": event,
                "preflight_ts": ts,
                "balance_before": _get_nested(row, "preflight", "balance", "clientBalance"),
                "config": _get_nested(row, "preflight", "config") or {},
                "gpu": None,
                "cloud": None,
                "receipt": None,
                "result_metrics": None,
            }
        elif any(event.endswith(m) or m in event for m in TERMINAL_PRUNE_MARKERS):
            close("pruned", row, ts)
        elif any(event.endswith(m) for m in TERMINAL_SUCCESS_MARKERS):
            close("success", row, ts)
    if pending is not None:
        close("abandoned", None, pending.get("preflight_ts"))
    return attempts


def classify(attempt: Dict[str, Any]) -> str:
    """Workload class of a successful attempt."""
    if attempt["ledger"] != "integrated_spec_render_token_ledger.jsonl":
        return "single_frame_probe"
    rm = _get_nested(attempt.get("receipt") or {}, "render_metrics") or {}
    res = rm.get("resolution") or _get_nested(attempt, "config", "resolution") or ""
    repair = bool(rm.get("repair_enabled") or _get_nested(attempt, "config", "repair_enabled"))
    if "3840" in str(res):
        return "pipeline_4k_repair" if repair else "pipeline_4k"
    return "pipeline_1080p"


# ---------------------------------------------------------------------------
# Cost attribution + rate extraction
# ---------------------------------------------------------------------------

def attribute_costs(attempts: List[Dict[str, Any]],
                    snapshots: List[Tuple[datetime, float]],
                    basis_rates: Optional[Dict[str, float]] = None) -> None:
    """Attach measured (balance-delta) or modeled cost to each attempt, in place.

    cost = balance_at_own_preflight - first_snapshot_at_or_after_terminal_event.
    The window may extend past the terminal event to the next snapshot; that
    slack is honest (termination lag + billing granularity) and is recorded.
    """
    for a in attempts:
        a["cost_usd"] = None
        a["cost_label"] = None
        a["session_hours"] = None
        a["implied_usd_hr"] = None
        pre_ts, end_ts = a.get("preflight_ts"), a.get("end_ts")
        bal_before = a.get("balance_before")
        if pre_ts is not None and end_ts is not None:
            a["session_hours"] = max((end_ts - pre_ts).total_seconds(), 0.0) / 3600.0
        if pre_ts is None or end_ts is None or not isinstance(bal_before, (int, float)):
            continue
        after = next((s for s in snapshots if s[0] >= end_ts and s[0] > pre_ts), None)
        if after is not None:
            delta = float(bal_before) - after[1]
            if delta >= 0:  # a negative delta means a top-up crossed the window
                a["cost_usd"] = round(delta, 4)
                a["cost_label"] = "MEASURED (balance-delta)"
                a["cost_window_end"] = after[0].isoformat()
                if a["session_hours"] and a["session_hours"] >= MIN_SESSION_HOURS_FOR_RATE:
                    a["implied_usd_hr"] = round(delta / a["session_hours"], 3)
        if a["cost_usd"] is None and basis_rates and a.get("gpu") in basis_rates \
                and a.get("session_hours"):
            a["cost_usd"] = round(basis_rates[a["gpu"]] * a["session_hours"], 4)
            a["cost_label"] = "MODELED (session_h x basis $/hr; no closing balance snapshot)"


def derive_rates(attempts: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per-GPU $/hr: extracted median of implied rates where possible, else the
    documented constant."""
    samples: Dict[str, List[float]] = {}
    for a in attempts:
        if a.get("outcome") == "success" and a.get("implied_usd_hr") and a.get("gpu"):
            samples.setdefault(a["gpu"], []).append(a["implied_usd_hr"])
    rates: Dict[str, Dict[str, Any]] = {}
    keys = set(samples) | set(DOCUMENTED_RATES_USD_HR)
    for k in sorted(keys):
        vals = sorted(samples.get(k, []))
        documented = DOCUMENTED_RATES_USD_HR.get(k)
        extracted = round(statistics.median(vals), 3) if vals else None
        if extracted is not None and len(vals) >= MIN_RATE_SAMPLES:
            basis, source = extracted, "extracted-median (MEASURED-derived)"
        elif documented is not None:
            basis, source = documented, "documented constant"
        else:
            basis, source = extracted, "extracted-single-sample (MEASURED-derived, n=1)"
        rates[k] = {
            "documented_usd_hr": documented,
            "extracted_median_usd_hr": extracted,
            "extracted_samples": vals,
            "n_samples": len(vals),
            "basis_usd_hr": basis,
            "basis_source": source,
        }
    return rates


# ---------------------------------------------------------------------------
# Quote building
# ---------------------------------------------------------------------------

def _median_or_none(vals: List[float]) -> Optional[float]:
    return round(statistics.median(vals), 4) if vals else None


def _summarize_class(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    costs = [r["cost_usd"] for r in runs if r.get("cost_usd") is not None
             and str(r.get("cost_label", "")).startswith("MEASURED")]
    modeled = [r["cost_usd"] for r in runs if r.get("cost_usd") is not None
               and str(r.get("cost_label", "")).startswith("MODELED")]
    sessions = [r["session_hours"] for r in runs if r.get("session_hours")]
    return {
        "n_runs": len(runs),
        "gpus": sorted({r["gpu"] for r in runs if r.get("gpu")}),
        "measured_costs_usd": sorted(round(c, 2) for c in costs),
        "modeled_costs_usd": sorted(round(c, 2) for c in modeled),
        "median_measured_cost_usd": _median_or_none(costs),
        "median_session_min": round(statistics.median(sessions) * 60.0, 1) if sessions else None,
        "session_min_range": [round(min(sessions) * 60, 1), round(max(sessions) * 60, 1)] if sessions else None,
    }


def build_report(ledger_dir: Path, sweep_ns: List[int]) -> Dict[str, Any]:
    rows_by_ledger = {name: parse_jsonl(ledger_dir / name) for name in LEDGER_FILES}
    snapshots = extract_snapshots(rows_by_ledger)

    attempts: List[Dict[str, Any]] = []
    for name, rows in rows_by_ledger.items():
        attempts.extend(extract_attempts(rows, name))
    attempts.sort(key=lambda a: a.get("preflight_ts") or datetime.max.replace(tzinfo=None).astimezone())

    # Pass 1: measured costs only -> rates; pass 2: model the stragglers.
    attribute_costs(attempts, snapshots)
    rates = derive_rates(attempts)
    basis_rates = {k: v["basis_usd_hr"] for k, v in rates.items() if v["basis_usd_hr"]}
    attribute_costs(attempts, snapshots, basis_rates)

    successes = [a for a in attempts if a["outcome"] == "success"]
    failures = [a for a in attempts if a["outcome"] in ("pruned", "abandoned")]
    for a in successes:
        a["workload_class"] = classify(a)

    by_class: Dict[str, List[Dict[str, Any]]] = {}
    for a in successes:
        by_class.setdefault(a["workload_class"], []).append(a)

    # --- failed-attempt overhead (MEASURED) --------------------------------
    waste_measured = [a["cost_usd"] for a in failures
                      if a.get("cost_usd") is not None
                      and str(a.get("cost_label", "")).startswith("MEASURED")]
    success_measured = [a["cost_usd"] for a in successes
                        if a.get("cost_usd") is not None
                        and str(a.get("cost_label", "")).startswith("MEASURED")]
    waste_total = round(sum(waste_measured), 2)
    success_total = round(sum(success_measured), 2)
    waste_pct = round(100.0 * waste_total / success_total, 1) if success_total else None

    # --- MEASURED component times for the MODELED rows ----------------------
    # Session overhead = session wall minus in-receipt render wall (provision +
    # upload + probe + scoring + terminate-to-snapshot slack).
    overheads_h: List[float] = []
    for a in successes:
        rc = a.get("receipt") or {}
        base_s, spec_s = rc.get("baseline_total_s"), rc.get("spec_total_s")
        if a.get("session_hours") and isinstance(base_s, (int, float)) and isinstance(spec_s, (int, float)):
            oh = a["session_hours"] - (base_s + spec_s) / 3600.0
            if oh > 0:
                overheads_h.append(oh)
    overhead_h = _median_or_none(overheads_h)

    ref_frame_s: List[float] = []   # 1080p reference frame seconds (per frame)
    anchor_s: List[float] = []      # 1080p anchor (draft-stack) frame seconds
    scene_render_h: List[float] = []  # full 1080p pipeline render wall (baseline+spec)
    for a in by_class.get("pipeline_1080p", []):
        rm = _get_nested(a.get("receipt") or {}, "render_metrics") or {}
        pf = rm.get("per_frame_ref_s")
        if isinstance(pf, list) and pf and all(isinstance(x, (int, float)) for x in pf):
            ref_frame_s.append(statistics.mean(pf))
        if isinstance(rm.get("mean_keyframe_render_s"), (int, float)):
            anchor_s.append(rm["mean_keyframe_render_s"])
        rc = a.get("receipt") or {}
        if isinstance(rc.get("baseline_total_s"), (int, float)) and isinstance(rc.get("spec_total_s"), (int, float)):
            scene_render_h.append((rc["baseline_total_s"] + rc["spec_total_s"]) / 3600.0)
    med_ref_frame_s = _median_or_none(ref_frame_s)
    med_anchor_s = _median_or_none(anchor_s)
    med_scene_render_h = _median_or_none(scene_render_h)

    # --- quote rows ---------------------------------------------------------
    quotes: List[Dict[str, Any]] = []

    def measured_quote(workload: str, cls: str, note: str) -> None:
        runs = by_class.get(cls, [])
        s = _summarize_class(runs)
        if not runs:
            quotes.append({"workload": workload, "label": "NO DATA", "note": note})
            return
        quotes.append({
            "workload": workload,
            "label": "MEASURED (balance-delta)" if s["measured_costs_usd"] else "MODELED",
            "quote_usd": s["median_measured_cost_usd"],
            "quote_range_usd": [min(s["measured_costs_usd"]), max(s["measured_costs_usd"])] if s["measured_costs_usd"] else None,
            "wall_min_median": s["median_session_min"],
            "wall_min_range": s["session_min_range"],
            "gpus": s["gpus"],
            "n_measured": len(s["measured_costs_usd"]),
            "n_modeled": len(s["modeled_costs_usd"]),
            "modeled_costs_usd": s["modeled_costs_usd"] or None,
            "note": note,
        })

    measured_quote(
        "1080p 4-frame pipeline / scene (ref 1536 / draft 192)",
        "pipeline_1080p",
        "classroom; A100 and H100 each measured once — the range IS the per-GPU spread",
    )
    measured_quote(
        "4K 4-frame pipeline, no repair / scene (ref 4096 / draft 512)",
        "pipeline_4k",
        "classroom, A100 (RUN 3, the 5.561x headline)",
    )
    measured_quote(
        "4K strict-delivery w/ repair / scene (ref 4096 / draft 512 + tile repair)",
        "pipeline_4k_repair",
        "classroom, H100 (RUNs 4-7 incl. the DECISION=GROW strict pass; RUN 7 cost modeled — no closing snapshot)",
    )
    measured_quote(
        "single-frame probe (draft(s) + 4K reference + scoring)",
        "single_frame_probe",
        "cross-denoiser / multi-selector / reference-consistency probes",
    )

    # Cross-arch CUDA half: MODELED from MEASURED components.
    cross_arch: Dict[str, Any] = {
        "workload": "cross-arch CUDA half (1 frame 1080p: reference + anchor draft)",
        "label": "MODELED from MEASURED components",
        "note": "session = median measured overhead + 1x 1080p ref frame + 1x anchor frame; "
                "components MEASURED, the sum-x-rate is MODELED",
    }
    if overhead_h is not None and med_ref_frame_s is not None and med_anchor_s is not None:
        hours = overhead_h + (med_ref_frame_s + med_anchor_s) / 3600.0
        cross_arch["modeled_session_min"] = round(hours * 60.0, 1)
        cross_arch["quote_by_gpu_usd"] = {
            g: round(hours * basis_rates[g], 2) for g in ("A100", "H100") if g in basis_rates
        }
        cross_arch["components"] = {
            "overhead_h_median": overhead_h,
            "ref_frame_s_median_1080p": med_ref_frame_s,
            "anchor_frame_s_median_1080p": med_anchor_s,
        }
    else:
        cross_arch["label"] = "NO DATA (components missing from ledgers)"
    quotes.append(cross_arch)

    # Scene sweep: MODELED warm-pod formula + conservative cold-pod bound.
    sweep: Dict[str, Any] = {
        "workload": "scene sweep @1080p, N scenes (one warm pod)",
        "label": "MODELED from MEASURED components",
        "note": "warm: rate x (overhead + N x per-scene render wall); per-scene wall is the "
                "classroom MEASURED 1080p pipeline wall — unseen scenes vary. cold bound: "
                "N x measured per-scene cost",
    }
    p1080 = _summarize_class(by_class.get("pipeline_1080p", []))
    if overhead_h is not None and med_scene_render_h is not None and "A100" in basis_rates:
        rate = basis_rates["A100"]
        sweep["formula"] = (
            f"cost(N) = {rate:.2f} $/hr x ({overhead_h:.3f} h + N x {med_scene_render_h:.4f} h)  [A100 basis]"
        )
        sweep["warm_pod_usd_by_n"] = {
            str(n): round(rate * (overhead_h + n * med_scene_render_h), 2) for n in sweep_ns
        }
        if p1080["median_measured_cost_usd"]:
            sweep["cold_pod_usd_by_n"] = {
                str(n): round(n * p1080["median_measured_cost_usd"], 2) for n in sweep_ns
            }
    else:
        sweep["label"] = "NO DATA (components missing from ledgers)"
    quotes.append(sweep)

    notes = [
        "Costs are RunPod balance deltas: balance at the run's own preflight minus the first "
        "snapshot at/after its terminal event — they INCLUDE provisioning, setup, probe, scoring "
        "and termination/billing slack to the next snapshot. That slack points the conservatism "
        "the right way (quotes err high, not low).",
        f"Failed-attempt overhead is real and MEASURED: ${waste_total} across {len(waste_measured)} "
        f"pruned/abandoned attempts vs ${success_total} of successful-run spend"
        + (f" ({waste_pct}% overhead). Quote sequenced cloud runs with a ~{max(25, round((waste_pct or 0) / 5) * 5)}% "
           f"contingency for capacity crunches (the 07-09 17:30-19:20 A100/H100/H200 drought alone burned "
           f"~$4.5 in provisioning attempts)." if waste_pct is not None else "."),
        "Per-scene render walls are classroom-specific; a scene sweep over unseen scenes inherits "
        "the classroom wall only as a basis, not a promise.",
        "LOCAL ONLY: this estimator reads ledgers on disk; it never calls the RunPod API.",
    ]

    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "ledger_dir": str(ledger_dir),
        "n_balance_snapshots": len(snapshots),
        "rates_usd_hr": rates,
        "runs": [
            {
                "ledger": a["ledger"],
                "workload_class": a.get("workload_class"),
                "outcome": a["outcome"],
                "gpu": a.get("gpu"),
                "cloud": a.get("cloud"),
                "preflight_ts": a["preflight_ts"].isoformat() if a.get("preflight_ts") else None,
                "end_ts": a["end_ts"].isoformat() if a.get("end_ts") else None,
                "session_hours": round(a["session_hours"], 4) if a.get("session_hours") else None,
                "cost_usd": a.get("cost_usd"),
                "cost_label": a.get("cost_label"),
                "implied_usd_hr": a.get("implied_usd_hr"),
            }
            for a in attempts
        ],
        "failed_attempt_overhead": {
            "n_attempts": len(failures),
            "n_with_measured_cost": len(waste_measured),
            "measured_usd": waste_total,
            "successful_measured_usd": success_total,
            "pct_of_successful_spend": waste_pct,
            "label": "MEASURED (balance-delta)",
        },
        "quotes": quotes,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def _fmt_usd(v: Optional[float]) -> str:
    return f"${v:.2f}" if isinstance(v, (int, float)) else "—"


def render_markdown(report: Dict[str, Any]) -> str:
    out: List[str] = []
    out.append("# RunPod cost quote (derived from measured campaign ledgers)")
    out.append("")
    out.append(f"Generated {report['generated_at']} from `{report['ledger_dir']}` "
               f"({report['n_balance_snapshots']} balance snapshots).")
    out.append("")
    out.append("## Per-GPU $/hr basis")
    out.append("")
    out.append("| GPU | documented $/hr | extracted median $/hr (n) | quote basis | source |")
    out.append("|---|---|---|---|---|")
    for gpu, r in report["rates_usd_hr"].items():
        ex = f"{r['extracted_median_usd_hr']:.2f} (n={r['n_samples']})" if r["extracted_median_usd_hr"] else "—"
        doc = f"{r['documented_usd_hr']:.2f}" if r["documented_usd_hr"] else "—"
        basis = f"{r['basis_usd_hr']:.2f}" if r["basis_usd_hr"] else "—"
        out.append(f"| {gpu} | {doc} | {ex} | **{basis}** | {r['basis_source']} |")
    out.append("")
    out.append("## Quote table")
    out.append("")
    out.append("| workload | quote | measured range | wall (median) | GPUs | label |")
    out.append("|---|---|---|---|---|---|")
    for q in report["quotes"]:
        if "quote_usd" in q:
            rng = q.get("quote_range_usd")
            rng_s = f"{_fmt_usd(rng[0])}–{_fmt_usd(rng[1])}" if rng else "—"
            wall = f"{q['wall_min_median']:.0f} min" if q.get("wall_min_median") else "—"
            gpus = "/".join(q.get("gpus") or []) or "—"
            n = f"{q['label']} (n={q.get('n_measured', 0)}"
            n += f"+{q['n_modeled']} modeled)" if q.get("n_modeled") else ")"
            out.append(f"| {q['workload']} | **{_fmt_usd(q.get('quote_usd'))}** | {rng_s} | {wall} | {gpus} | {n} |")
        elif "quote_by_gpu_usd" in q:
            by = ", ".join(f"{g} {_fmt_usd(v)}" for g, v in q["quote_by_gpu_usd"].items())
            wall = f"{q['modeled_session_min']:.0f} min" if q.get("modeled_session_min") else "—"
            out.append(f"| {q['workload']} | **{by}** | — | {wall} | "
                       f"{'/'.join(q['quote_by_gpu_usd'].keys())} | {q['label']} |")
        elif "warm_pod_usd_by_n" in q:
            warm = ", ".join(f"N={n} {_fmt_usd(v)}" for n, v in q["warm_pod_usd_by_n"].items())
            cold = q.get("cold_pod_usd_by_n")
            cold_s = ("; cold bound " + ", ".join(f"N={n} {_fmt_usd(v)}" for n, v in cold.items())) if cold else ""
            out.append(f"| {q['workload']} | **{warm}**{cold_s} | — | — | A100 | {q['label']} |")
        else:
            out.append(f"| {q['workload']} | — | — | — | — | {q['label']} |")
    out.append("")
    fo = report["failed_attempt_overhead"]
    out.append(f"Failed-attempt overhead (MEASURED): **{_fmt_usd(fo['measured_usd'])}** across "
               f"{fo['n_with_measured_cost']} pruned/abandoned attempts "
               f"vs {_fmt_usd(fo['successful_measured_usd'])} successful spend"
               + (f" = **{fo['pct_of_successful_spend']}%** — add contingency when sequencing runs."
                  if fo["pct_of_successful_spend"] is not None else "."))
    out.append("")
    out.append("## Notes")
    out.append("")
    for n in report["notes"]:
        out.append(f"- {n}")
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def default_ledger_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "docs" / "speed-lane-reports" / "spec-lab"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ledger-dir", type=Path, default=None,
                    help="directory containing the campaign *.jsonl ledgers")
    ap.add_argument("--json-out", type=Path, default=None, help="write full JSON report here")
    ap.add_argument("--md-out", type=Path, default=None, help="write markdown report here")
    ap.add_argument("--sweep-n", type=int, nargs="+", default=[3, 4, 6],
                    help="scene-sweep sizes to quote (default: 3 4 6)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    args = ap.parse_args(argv)

    ledger_dir = args.ledger_dir or default_ledger_dir()
    if not ledger_dir.is_dir():
        print(f"error: ledger dir not found: {ledger_dir}", file=sys.stderr)
        return 2

    report = build_report(ledger_dir, args.sweep_n)
    md = render_markdown(report)
    if args.json_out:
        args.json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.md_out:
        args.md_out.write_text(md, encoding="utf-8")
    print(json.dumps(report, indent=2) if args.json else md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
