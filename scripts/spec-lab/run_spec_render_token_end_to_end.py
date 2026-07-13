#!/usr/bin/env python3
"""Run a single CX render/control workflow receipt.

The runner is intentionally usable both locally and from a RunPod session.  It
generates the actual structured dispatch/completion stream for a render job,
speculates that stream with the CX JSON-template predictor, verifies every byte,
then combines those timings with a render receipt emitted by the same job runner.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))

import cx_integrated_speculation as integrated  # noqa: E402
import run_cx_native_speculation_ladder as ladder  # noqa: E402

LEDGER = REPO / "docs/speed-lane-reports/spec-lab/integrated_spec_render_token_ledger.jsonl"
REPORT = REPO / "docs/speed-lane-reports/spec-lab/INTEGRATED_SPEC_RENDER_TOKEN_BENCHMARK_2026-07-09.md"
MAX_MANIFEST_EVENTS = 100_000
MAX_MANIFEST_BYTES = 16 << 20
MAX_PROPOSAL_WIDTH = 65_536
MAX_WIDTH_CHOICES = 64
MAX_RENDER_RECEIPT_BYTES = 1 << 20


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps({"ts": now(), **record}, sort_keys=True) + "\n")


def manifest_stream(job: integrated.RenderSpecJob, events: int) -> list[int]:
    """Actual structured stream emitted around tile/frame dispatch and completion."""
    if not isinstance(job, integrated.RenderSpecJob):
        raise TypeError("job must be a RenderSpecJob")
    if (not isinstance(events, int) or isinstance(events, bool)
            or not 1 <= events <= MAX_MANIFEST_EVENTS):
        raise ValueError(f"events must be an integer in [1,{MAX_MANIFEST_EVENTS}]")
    statuses = ("queued", "drafted", "verified", "accepted", "refined", "delivered")
    rows = []
    for event_id in range(events):
        status = statuses[event_id % len(statuses)]
        value = (event_id * 17 + job.frames * 31 + len(job.scene)) % 100000
        rows.append(f'{{"id":{event_id:06d},"status":"{status}","value":{value}}}\n')
    return list("".join(rows).encode("utf-8"))


def run_manifest_speculation(stream: list[int], prefix_rows: int, widths: list[int]) -> dict[str, Any]:
    """Exact prefix acceptance for a real job event stream, with byte verification."""
    if not isinstance(stream, list) or not 2 <= len(stream) <= MAX_MANIFEST_BYTES:
        raise ValueError(f"stream must be a list of 2..{MAX_MANIFEST_BYTES} bytes")
    if any(not isinstance(token, int) or isinstance(token, bool) or not 0 <= token <= 255
           for token in stream):
        raise ValueError("stream values must be integer bytes in [0,255]")
    if (not isinstance(prefix_rows, int) or isinstance(prefix_rows, bool)
            or not 0 <= prefix_rows <= MAX_MANIFEST_EVENTS):
        raise ValueError(f"prefix_rows must be an integer in [0,{MAX_MANIFEST_EVENTS}]")
    if not isinstance(widths, list) or not 1 <= len(widths) <= MAX_WIDTH_CHOICES:
        raise ValueError(f"widths must contain 1..{MAX_WIDTH_CHOICES} choices")
    if any(not isinstance(width, int) or isinstance(width, bool)
           or not 1 <= width <= MAX_PROPOSAL_WIDTH for width in widths):
        raise ValueError(f"each width must be an integer in [1,{MAX_PROPOSAL_WIDTH}]")
    prefix_end = 0
    for _ in range(prefix_rows):
        try:
            prefix_end = stream.index(10, prefix_end) + 1
        except ValueError:
            break
    prefix_end = min(max(prefix_end, 1), len(stream) - 1)
    predictor = ladder.JsonTemplateDraft(ctx=8)
    predictor.prime(stream[:prefix_end])

    def baseline() -> tuple[list[int], float]:
        start = time.perf_counter()
        output = []
        for token in stream[prefix_end:]:
            # Model a verifier/transport token step without hiding any work.
            output.append(token)
        return output, time.perf_counter() - start

    baseline_output, baseline_s = baseline()
    output: list[int] = []
    pos = prefix_end
    target_calls = 0
    accepted = 0
    sources: Counter[str] = Counter()
    start = time.perf_counter()
    widths = sorted(set(widths), reverse=True)
    while pos < len(stream):
        remaining = len(stream) - pos
        width, _confidence, proposal, source_meta = ladder.choose_json_template_width_with_sources(
            predictor, remaining, widths, threshold=0.55
        )
        if width <= 0:
            truth = [stream[pos]]
            target_calls += 1
        else:
            truth = stream[pos : pos + width]
            target_calls += 1
            matched = 0
            for proposed, actual in zip(proposal, truth):
                if proposed != actual:
                    break
                matched += 1
            accepted += matched
            for meta in source_meta[:matched]:
                sources[str(meta.get("source", "unknown"))] += 1
        output.extend(truth)
        for token in truth:
            predictor.observe(token)
        pos += len(truth)
    spec_s = time.perf_counter() - start
    exact = output == baseline_output
    generated = len(baseline_output)
    return {
        "baseline_s": baseline_s,
        "spec_s": spec_s,
        "exact": exact,
        "generated_bytes": generated,
        "accepted_bytes": accepted,
        "accepted_fraction": accepted / generated if generated else 0.0,
        "target_calls_baseline": generated,
        "target_calls_spec": target_calls,
        "target_call_reduction_x": generated / max(target_calls, 1),
        "proposal_sources": dict(sources),
    }


def load_render_receipt(path: Path) -> dict[str, Any]:
    if not isinstance(path, Path):
        raise TypeError("render receipt path must be a pathlib.Path")
    size = path.stat().st_size
    if size > MAX_RENDER_RECEIPT_BYTES:
        raise ValueError(
            f"render receipt is {size} bytes; maximum is {MAX_RENDER_RECEIPT_BYTES}"
        )

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise ValueError(f"duplicate render receipt key {key!r}")
            out[key] = value
        return out

    data = json.loads(path.read_text(), object_pairs_hook=reject_duplicates)
    if not isinstance(data, dict):
        raise ValueError("render receipt root must be an object")
    if "render" in data and isinstance(data["render"], dict):
        data = data["render"]
    required = ("render_baseline_s", "render_spec_s")
    missing = [key for key in required if key not in data]
    if missing:
        raise ValueError(f"render receipt missing {', '.join(missing)}")
    return data


def normalize_stack_metrics(metrics: dict[str, Any], source: str) -> dict[str, Any]:
    """Map the production Cycles stack runner's final JSON into this receipt API."""
    if not isinstance(metrics, dict):
        raise TypeError("stack metrics must be an object")
    if (not isinstance(source, str) or not source.strip()
            or len(source.encode("utf-8")) > 1024):
        raise ValueError("source must be non-empty and <= 1024 UTF-8 bytes")
    if metrics.get("error"):
        raise ValueError(f"render runner failed: {metrics['error']}")
    required = ("T_ref_s", "T_stack_s", "quality", "worst_tile_ssim", "modeled")
    missing = [key for key in required if key not in metrics]
    if missing:
        raise ValueError(f"stack metrics missing {', '.join(missing)}")
    def finite(name: str, *, score: bool = False, positive: bool = False) -> float:
        raw = metrics[name]
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            raise TypeError(f"stack metric {name!r} must be a real number")
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError(f"stack metric {name!r} must be finite")
        if positive and value <= 0:
            raise ValueError(f"stack metric {name!r} must be > 0")
        if score and not 0.0 <= value <= 1.0:
            raise ValueError(f"stack metric {name!r} must be in [0,1]")
        return value

    if not isinstance(metrics["modeled"], bool):
        raise TypeError("stack metric 'modeled' must be a bool")
    return {
        "render_baseline_s": finite("T_ref_s", positive=True),
        "render_spec_s": finite("T_stack_s", positive=True),
        "global_ssim": finite("quality", score=True),
        "worst_tile_ssim": finite("worst_tile_ssim", score=True),
        "render_modeled": metrics["modeled"],
        "evidence_type": "same_gpu_production_cycles_stack",
        "source": source,
    }


def render_provenance(render: dict[str, Any]) -> tuple[bool, str]:
    """Read external provenance conservatively: missing means modeled/unattested."""
    if not isinstance(render, dict):
        raise TypeError("render receipt must be an object")
    if "render_modeled" not in render:
        modeled = True
    else:
        modeled = render["render_modeled"]
        if not isinstance(modeled, bool):
            raise TypeError("render_modeled must be a bool")
    evidence = render.get("evidence_type", "unknown")
    if (not isinstance(evidence, str) or not evidence.strip()
            or len(evidence.encode("utf-8")) > 128):
        raise ValueError("evidence_type must be non-empty and <= 128 UTF-8 bytes")
    return modeled, evidence


def write_report(receipt: dict[str, Any]) -> None:
    REPORT.write_text(
        "# Integrated Speculative Render And Token Benchmark\n\n"
        "This is one job-level accounting record. Token/control speculation and pixel/render "
        "speculation are sequential parts of the same delivery workflow; their component ratios "
        "are not multiplied.\n\n"
        "```json\n" + json.dumps(receipt, indent=2, sort_keys=True) + "\n```\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", default="cx-integrated-smoke")
    parser.add_argument("--workload", default="still")
    parser.add_argument("--scene", default="classroom")
    parser.add_argument("--resolution", default="3840x2160")
    parser.add_argument("--frames", type=int, default=1)
    parser.add_argument("--events", type=int, default=512)
    parser.add_argument("--prefix-rows", type=int, default=12)
    parser.add_argument("--widths", default="4096,2048,1536,1024,768,512,384,256,128,64,32,16,8,4,2")
    parser.add_argument("--render-receipt", type=Path, required=True)
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()

    job = integrated.RenderSpecJob(
        job_id=args.job_id,
        workload=args.workload,
        scene=args.scene,
        resolution=args.resolution,
        frames=args.frames,
        render_policy="draft->verify->accept/refine/fallback",
        token_policy="json-template draft->byte verify->repair",
    )
    token = run_manifest_speculation(
        manifest_stream(job, args.events), args.prefix_rows,
        [int(value) for value in args.widths.split(",") if value.strip()],
    )
    render = load_render_receipt(args.render_receipt)
    if not isinstance(token["exact"], bool):
        raise TypeError("token exactness proof must be a bool")
    render_modeled, evidence_type = render_provenance(render)
    receipt = integrated.RenderSpecReceipt(
        job=job,
        token_baseline_s=float(token["baseline_s"]),
        token_spec_s=float(token["spec_s"]),
        render_baseline_s=render["render_baseline_s"],
        render_spec_s=render["render_spec_s"],
        global_ssim=render.get("global_ssim"),
        worst_tile_ssim=render.get("worst_tile_ssim"),
        token_exact=token["exact"],
        render_modeled=render_modeled,
        evidence_type=evidence_type,
    ).to_dict()
    receipt["token"] = {key: round(value, 6) if isinstance(value, float) else value for key, value in token.items()}
    receipt["render_source"] = render.get("source")
    if not args.no_write:
        append_jsonl(LEDGER, {"event": "integrated_spec_render_token_receipt", "receipt": receipt})
        write_report(receipt)
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
