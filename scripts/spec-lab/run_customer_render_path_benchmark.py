#!/usr/bin/env python3
"""Replay render-frontier receipts into an explicitly modeled customer-path view.

This is a lab-only decision aid.  It validates the native closure receipts before
using their numbers, keeps their distinct benchmark domains visible, and labels
every cross-receipt or workload calculation as modeled.  It does not execute a
render, authorize an artifact, quote a customer, or make anything billable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys
from typing import Any, Iterable, Sequence


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import run_spatial75_cycles_frontier as spatial_frontier  # noqa: E402
import customer_render_policy_v1 as customer_policy  # noqa: E402
import screen_spatial75_one_render_upper_bound as one_render_frontier  # noqa: E402
import screen_static_frame_cache_reuse as cache_frontier  # noqa: E402
import screen_temporal_prediction_frontier as temporal_frontier  # noqa: E402


SCHEMA_VERSION = 1
KIND = "cx_render_customer_path_tradeoff_model"
DEFAULT_HIT_RATES = (
    0.0,
    0.5,
    0.9,
    0.95,
    0.99,
    0.999,
    0.9999,
    1.0,
)
DEFAULT_SHARED_OVERHEADS_MS = (0.0, 50.0, 100.0, 200.0, 500.0, 1000.0)
DEFAULT_TARGET_SPEEDUPS = (2.0, 10.0, 100.0, 1000.0)
DEFAULT_LINK_RATES_MBPS = (25.0, 50.0, 100.0, 500.0, 1000.0)
MAX_EVIDENCE_BYTES = 16 << 20


class CustomerPathModelError(RuntimeError):
    """Raised when evidence or a modeling input is not fail-closed."""


def canonical_json(value: Any, *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CustomerPathModelError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _evidence_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
        info.st_gid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _reject_json_constant(value: str) -> None:
    raise CustomerPathModelError(f"non-finite JSON constant {value!r}")


def _read_json_snapshot(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        observed = path.lstat()
    except OSError as exc:
        raise CustomerPathModelError(f"evidence file is unavailable: {path}") from exc
    if path.is_symlink() or not stat.S_ISREG(observed.st_mode):
        raise CustomerPathModelError(f"evidence must be a regular non-symlink file: {path}")
    if not 1 <= observed.st_size <= MAX_EVIDENCE_BYTES:
        raise CustomerPathModelError("evidence byte length is outside the closed bound")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CustomerPathModelError(f"evidence cannot be opened safely: {path}") from exc
    raw = bytearray()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or _evidence_identity(
            before
        ) != _evidence_identity(observed):
            raise CustomerPathModelError("evidence identity changed before reading")
        while len(raw) <= MAX_EVIDENCE_BYTES:
            chunk = os.read(
                descriptor,
                min(1 << 20, MAX_EVIDENCE_BYTES + 1 - len(raw)),
            )
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        current = path.lstat()
        if (
            _evidence_identity(before) != _evidence_identity(after)
            or _evidence_identity(after) != _evidence_identity(current)
            or len(raw) != after.st_size
        ):
            raise CustomerPathModelError("evidence changed while reading")
    except OSError as exc:
        raise CustomerPathModelError("evidence changed while reading") from exc
    finally:
        os.close(descriptor)
    if len(raw) > MAX_EVIDENCE_BYTES:
        raise CustomerPathModelError("evidence exceeds the closed byte bound")
    data = bytes(raw)
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_strict_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CustomerPathModelError(f"evidence JSON is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise CustomerPathModelError(f"evidence root must be an object: {path}")
    canonical_json(value)
    return value, data


def read_json_object(path: Path) -> dict[str, Any]:
    value, _ = _read_json_snapshot(path)
    return value


def _assert_snapshot_current(path: Path, expected: bytes) -> None:
    _, current = _read_json_snapshot(path)
    if current != expected:
        raise CustomerPathModelError("evidence changed after validation")


def _finite_number(value: Any, label: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CustomerPathModelError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise CustomerPathModelError(f"{label} must be finite")
    if minimum is not None and result < minimum:
        raise CustomerPathModelError(f"{label} must be >= {minimum}")
    return result


def _validate_rates(values: Iterable[float], label: str) -> list[float]:
    result = [_finite_number(value, label) for value in values]
    if not result:
        raise CustomerPathModelError(f"{label} must not be empty")
    if any(value < 0.0 or value > 1.0 for value in result):
        raise CustomerPathModelError(f"{label} values must be in [0, 1]")
    if len(set(result)) != len(result):
        raise CustomerPathModelError(f"{label} values must be unique")
    return sorted(result)


def _validate_nonnegative(values: Iterable[float], label: str) -> list[float]:
    result = [_finite_number(value, label, minimum=0.0) for value in values]
    if not result:
        raise CustomerPathModelError(f"{label} must not be empty")
    if len(set(result)) != len(result):
        raise CustomerPathModelError(f"{label} values must be unique")
    return sorted(result)


def _validate_positive(values: Iterable[float], label: str) -> list[float]:
    result = [_finite_number(value, label) for value in values]
    if not result or any(value <= 0.0 for value in result):
        raise CustomerPathModelError(f"{label} values must be > 0")
    if len(set(result)) != len(result):
        raise CustomerPathModelError(f"{label} values must be unique")
    return sorted(result)


def parse_csv_numbers(raw: str, label: str) -> list[float]:
    if not isinstance(raw, str) or not raw.strip():
        raise CustomerPathModelError(f"{label} must not be empty")
    values: list[float] = []
    for part in raw.split(","):
        try:
            value = float(part.strip())
        except ValueError as exc:
            raise CustomerPathModelError(f"{label} contains a non-number") from exc
        values.append(value)
    return values


def two_point_quantile(
    hit_rate: float,
    hit_latency_s: float,
    miss_latency_s: float,
    quantile: float,
) -> float:
    hit_rate = _finite_number(hit_rate, "hit_rate")
    quantile = _finite_number(quantile, "quantile")
    hit_latency_s = _finite_number(hit_latency_s, "hit_latency_s", minimum=0.0)
    miss_latency_s = _finite_number(miss_latency_s, "miss_latency_s", minimum=0.0)
    if not 0.0 <= hit_rate <= 1.0 or not 0.0 < quantile <= 1.0:
        raise CustomerPathModelError("hit rate or quantile is outside its domain")
    if hit_latency_s > miss_latency_s:
        raise CustomerPathModelError("hit latency must not exceed miss latency")
    return hit_latency_s if quantile <= hit_rate else miss_latency_s


def required_hit_rate(
    baseline_s: float,
    hit_s: float,
    shared_overhead_s: float,
    target_speedup_x: float,
) -> dict[str, Any]:
    baseline_s = _finite_number(baseline_s, "baseline_s", minimum=0.0)
    hit_s = _finite_number(hit_s, "hit_s", minimum=0.0)
    overhead_s = _finite_number(
        shared_overhead_s, "shared_overhead_s", minimum=0.0
    )
    target_x = _finite_number(target_speedup_x, "target_speedup_x")
    if baseline_s <= hit_s:
        raise CustomerPathModelError("baseline must be slower than the hit path")
    if target_x < 1.0:
        raise CustomerPathModelError("target speedup must be >= 1")
    maximum_x = (baseline_s + overhead_s) / (hit_s + overhead_s)
    raw_rate = (
        (baseline_s + overhead_s) * (1.0 - 1.0 / target_x)
        / (baseline_s - hit_s)
    )
    reachable = raw_rate <= 1.0 + 1e-15 and target_x <= maximum_x + 1e-12
    return {
        "maximum_all_hit_speedup_x": maximum_x,
        "reachable": reachable,
        "required_hit_rate": max(0.0, raw_rate) if reachable else None,
        "required_hit_rate_percent": max(0.0, raw_rate) * 100.0
        if reachable
        else None,
        "target_speedup_x": target_x,
    }


def minimum_population_for_speedup(
    baseline_s: float, hit_s: float, target_speedup_x: float
) -> int | None:
    baseline_s = _finite_number(baseline_s, "baseline_s", minimum=0.0)
    hit_s = _finite_number(hit_s, "hit_s", minimum=0.0)
    target_x = _finite_number(target_speedup_x, "target_speedup_x")
    if baseline_s <= hit_s or target_x < 1.0:
        raise CustomerPathModelError("invalid population-amortization inputs")
    denominator = baseline_s - target_x * hit_s
    if denominator <= 0.0:
        return None
    raw = target_x * (baseline_s - hit_s) / denominator
    return max(1, math.ceil(raw - 1e-12))


def _receipt_provenance(path: Path, raw: bytes) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    return {
        "bytes": len(raw),
        "path": str(resolved),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def load_validated_receipts(
    *,
    cache_receipt_path: Path,
    spatial_receipt_path: Path,
    one_render_report_path: Path,
    temporal_receipt_path: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    cache_receipt, cache_raw = _read_json_snapshot(cache_receipt_path)
    spatial_receipt, spatial_raw = _read_json_snapshot(spatial_receipt_path)
    one_render_report, one_render_raw = _read_json_snapshot(one_render_report_path)
    temporal_receipt, temporal_raw = _read_json_snapshot(temporal_receipt_path)

    cache_frontier.validate_screen_receipt(
        cache_receipt, cache_receipt_path.resolve(strict=True).parent
    )
    spatial_frontier.validate_receipt(
        spatial_receipt, output_root=spatial_receipt_path.resolve(strict=True).parent
    )
    one_render_frontier.validate_closed_report(
        one_render_report, one_render_report_path.resolve(strict=True).parent
    )
    validated_temporal = temporal_frontier.validate_receipt_path(
        temporal_receipt_path.resolve(strict=True)
    )
    if canonical_json(validated_temporal) != canonical_json(temporal_receipt):
        raise CustomerPathModelError("temporal validator returned different evidence")

    cache_root = cache_receipt_path.resolve(strict=True).parent
    performance_record = cache_receipt["artifacts"]["source_performance_receipt"]
    performance_path = cache_root / performance_record["path"]
    baseline_provenance, baseline_raw = _read_json_snapshot(performance_path)
    if (
        len(baseline_raw) != performance_record["bytes"]
        or hashlib.sha256(baseline_raw).hexdigest() != performance_record["sha256"]
    ):
        raise CustomerPathModelError("cache baseline provenance changed")

    # Validators may inspect related artifacts and can take non-trivial time.  Bind
    # the model only if every input path still contains the exact byte snapshot that
    # was parsed, validated, and hashed above.  Any substitution fails the replay.
    for path, raw in (
        (cache_receipt_path, cache_raw),
        (spatial_receipt_path, spatial_raw),
        (one_render_report_path, one_render_raw),
        (temporal_receipt_path, temporal_raw),
        (performance_path, baseline_raw),
    ):
        _assert_snapshot_current(path, raw)

    receipts = {
        "cache_baseline_provenance": baseline_provenance,
        "exact_cache": cache_receipt,
        "spatial75_two_render": spatial_receipt,
        "spatial75_one_render": one_render_report,
        "temporal": temporal_receipt,
    }
    provenance = {
        "exact_cache": _receipt_provenance(cache_receipt_path, cache_raw),
        "spatial75_two_render": _receipt_provenance(spatial_receipt_path, spatial_raw),
        "spatial75_one_render": _receipt_provenance(
            one_render_report_path, one_render_raw
        ),
        "temporal": _receipt_provenance(temporal_receipt_path, temporal_raw),
    }
    return receipts, provenance


def extract_observed_evidence(
    receipts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    cache = receipts["exact_cache"]
    cache_baseline = receipts["cache_baseline_provenance"]
    spatial = receipts["spatial75_two_render"]
    one = receipts["spatial75_one_render"]
    temporal = receipts["temporal"]

    cache_head = cache["measurement"]["headline_scopes"]["exact_transport"]
    if cache_head["transport_authorized"] is not True:
        raise CustomerPathModelError("exact transport is not authorized by its receipt")
    if spatial["quality_pass"] is not True or spatial["preview_only"] is not True:
        raise CustomerPathModelError("Spatial75 evidence is not a passing preview")
    if one["authorization"] != {
        "independent_pair_gate_executed": False,
        "independent_verify_render_executed": False,
        "measurement_only": True,
        "production_change_authorized": False,
        "publication_authorized": False,
        "reason": "the second disjoint 4-SPP render and spatial75 agreement gate were deliberately excluded",
    }:
        raise CustomerPathModelError("one-render authorization boundary changed")
    if temporal["authorization"]["cross_frame_approximation_authorized"] is not False:
        raise CustomerPathModelError("temporal evidence unexpectedly became authorized")

    temporal_arms: list[dict[str, Any]] = []
    for arm in temporal["arms"]:
        row: dict[str, Any] = {
            "arm": arm["arm"],
            "authorization": arm["authorization"],
            "customer_selectable": False,
            "status": arm["status"],
        }
        if arm["status"] == "completed":
            row.update(
                {
                    "composed_estimate_s": arm["speed"][
                        "composed_charged_estimate"
                    ]["seconds"],
                    "composed_speedup_x": arm["speed"][
                        "composed_charged_estimate"
                    ]["speedup_x"],
                    "integrated_wall_measurement": arm["speed"][
                        "composed_charged_estimate"
                    ]["integrated_wall_measurement"],
                    "quality_pass": arm["quality_v3"]["pass"],
                }
            )
        temporal_arms.append(row)

    quality_selection = spatial["candidate"]["quality_trial_selection"]
    spatial_request_projection = {
        "candidate": {
            "draft_samples": spatial["candidate"]["draft_samples"],
            "draft_seed": spatial["candidate"]["draft_seed"],
            "resolution": spatial["candidate"]["resolution"],
            "sample_ranges": spatial["candidate"]["sample_ranges"],
            "verify_samples": spatial["candidate"]["verify_samples"],
            "verify_seed": spatial["candidate"]["verify_seed"],
        },
        "device": spatial["device"],
        "frame": spatial["frame"],
        "kind": "cx_spatial75_request_binding_v1",
        "pins": spatial["pins"],
        "renderer": {
            "executable_sha256": spatial["renderer_identity"][
                "executable_sha256"
            ],
            "runtime_bundle_sha256": spatial["renderer_identity"][
                "runtime_bundle"
            ]["sha256"],
        },
        "scene_sha256": spatial["scene"]["sha256"],
        "spatial75_policy_sha256": spatial["candidate"]["spatial75"]["gate"][
            "policy_sha256"
        ],
    }
    spatial_request_identity = hashlib.sha256(
        canonical_json(spatial_request_projection).encode("utf-8")
    ).hexdigest()
    return {
        "basic_cycles_frame9": {
            "local_wall_s": cache_head["baseline_s"],
            "receipt_trust": cache_baseline["receipt_trust"],
            "scope": "single pinned 4096-SPP comparator; not a customer SLA",
        },
        "exact_cache_frame9": {
            "artifact_bytes": cache_head["quality_identity"]["source_bytes"],
            "artifact_sha256": cache_head["quality_identity"]["source_sha256"],
            "local_median_s": cache["measurement"]["median_s"],
            "local_p95_s_type7": cache["measurement"]["p95_s_type7"],
            "local_slowest_s": cache["measurement"]["maximum_s"],
            "local_trial_count": cache["measurement"]["trial_count"],
            "request_identity": cache_head["request_cache_key"],
            "source_eligibility": cache_head["source_eligibility"],
            "transport_authorized": True,
            "scope": cache_head["scope"],
        },
        "basic_cycles_frame11": {
            "local_wall_s": spatial["baseline_s"],
            "receipt_trust": spatial["receipt_trust"],
            "scope": "single pinned 4096-SPP comparator; not a customer SLA",
        },
        "spatial75_two_render_frame11": {
            "customer_selectable_now": False,
            "experimental_preview_candidate": True,
            "local_median_s": spatial["timing_statistics"]["median_s"],
            "local_p95_s_type7": spatial["timing_statistics"]["p95_s_type7"],
            "local_slowest_s": spatial["timing_statistics"]["maximum_s"],
            "local_trial_count": spatial["trial_count"],
            "median_speedup_x": spatial["speedup_x"],
            "predeclared_quality_trial": quality_selection["trial_index"],
            "quality_pass": spatial["quality_pass"],
            "reference_free_pair_gate_pass": spatial["candidate"]["spatial75"][
                "gate"
            ]["passed"],
            "request_identity": spatial_request_identity,
            "scope": spatial["claim_scope"],
        },
        "spatial75_one_render_frame11": {
            "customer_selectable": False,
            "local_median_s": one["conclusion"]["median_wall_s"],
            "local_p95_s_type7": one["conclusion"]["p95_wall_s_type7"],
            "local_slowest_s": one["conclusion"]["slowest_wall_s"],
            "local_trial_count": one["trial_count"],
            "median_speedup_x": one["conclusion"]["median_speedup_x"],
            "quality_pass_count": one["conclusion"]["quality_v3_pass_count"],
            "reason": one["authorization"]["reason"],
        },
        "temporal_shadow_only": {
            "arms": temporal_arms,
            "customer_selectable": False,
            "experimental_only": temporal["experimental_only"],
            "receipt_trust": temporal["receipt_trust"],
        },
    }


def _exact_repeat_scenarios(
    *,
    baseline_s: float,
    hit_s: float,
    hit_rates: Sequence[float],
    overheads_s: Sequence[float],
    target_speedups: Sequence[float],
) -> dict[str, Any]:
    overhead_rows: list[dict[str, Any]] = []
    for overhead_s in overheads_s:
        baseline_customer_s = baseline_s + overhead_s
        hit_customer_s = hit_s + overhead_s
        scenarios: list[dict[str, Any]] = []
        for hit_rate in hit_rates:
            expected_local_s = hit_rate * hit_s + (1.0 - hit_rate) * baseline_s
            expected_customer_s = overhead_s + expected_local_s
            miss_latency_share = (
                (1.0 - hit_rate) * baseline_s / expected_local_s
                if expected_local_s > 0.0
                else 0.0
            )
            scenarios.append(
                {
                    "expected_customer_s": expected_customer_s,
                    "expected_local_s": expected_local_s,
                    "expected_speedup_x": baseline_customer_s / expected_customer_s,
                    "hit_rate": hit_rate,
                    "hit_rate_percent": hit_rate * 100.0,
                    "miss_latency_share_of_expected_local": miss_latency_share,
                    "misses_per_10000_requests": (1.0 - hit_rate) * 10000.0,
                    "p50_customer_s_two_point": overhead_s
                    + two_point_quantile(hit_rate, hit_s, baseline_s, 0.50),
                    "p95_customer_s_two_point": overhead_s
                    + two_point_quantile(hit_rate, hit_s, baseline_s, 0.95),
                    "p99_customer_s_two_point": overhead_s
                    + two_point_quantile(hit_rate, hit_s, baseline_s, 0.99),
                }
            )
        overhead_rows.append(
            {
                "all_hit_customer_s": hit_customer_s,
                "all_hit_speedup_x": baseline_customer_s / hit_customer_s,
                "basic_cycles_customer_s": baseline_customer_s,
                "hit_rate_scenarios": scenarios,
                "shared_overhead_s": overhead_s,
                "target_thresholds": [
                    required_hit_rate(baseline_s, hit_s, overhead_s, target)
                    for target in target_speedups
                ],
            }
        )
    return {
        "formula": "customer_latency = shared_overhead + h*cache_hit + (1-h)*full_cycles_miss",
        "model_kind": "two_point_expected_latency_and_nearest_rank_quantiles",
        "overhead_rows": overhead_rows,
        "population_charge": {
            "formula": "average_local = (one_origin_cycles + (N-1)*cache_hit) / N",
            "minimum_identical_requests_by_target": [
                {
                    "minimum_total_requests": minimum_population_for_speedup(
                        baseline_s, hit_s, target
                    ),
                    "target_speedup_x": target,
                }
                for target in target_speedups
            ],
        },
        "scope": "modeled workload; no measured hit-rate distribution or tail SLA",
    }


def _progressive_preview_model(
    *,
    baseline_s: float,
    spatial_s: float,
    one_render_s: float,
    overheads_s: Sequence[float],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for overhead_s in overheads_s:
        basic_customer_s = baseline_s + overhead_s
        gated_customer_s = spatial_s + overhead_s
        one_customer_s = one_render_s + overhead_s
        rows.append(
            {
                "basic_cycles_customer_s": basic_customer_s,
                "gated_preview_customer_s": gated_customer_s,
                "gated_preview_time_to_first_image_speedup_x": basic_customer_s
                / gated_customer_s,
                "one_render_customer_s": one_customer_s,
                "one_render_time_to_first_image_speedup_x": basic_customer_s
                / one_customer_s,
                "shared_overhead_s": overhead_s,
            }
        )
    return {
        "gated_preview_sequential_extra_compute_fraction": spatial_s / baseline_s,
        "gated_preview_sequential_extra_compute_percent": spatial_s
        / baseline_s
        * 100.0,
        "one_render_sequential_extra_compute_fraction": one_render_s / baseline_s,
        "one_render_sequential_extra_compute_percent": one_render_s
        / baseline_s
        * 100.0,
        "one_render_savings_vs_gated_preview_s": spatial_s - one_render_s,
        "one_render_savings_vs_gated_preview_percent": (spatial_s - one_render_s)
        / spatial_s
        * 100.0,
        "overhead_rows": rows,
        "scope": "modeled time-to-first-preview; full Cycles final time is not accelerated",
    }


def _cross_receipt_hybrid_model(
    *,
    baseline_s: float,
    cache_hit_s: float,
    spatial_miss_s: float,
    hit_rates: Sequence[float],
    target_speedups: Sequence[float],
) -> dict[str, Any]:
    scenarios = []
    for hit_rate in hit_rates:
        expected_s = hit_rate * cache_hit_s + (1.0 - hit_rate) * spatial_miss_s
        scenarios.append(
            {
                "expected_local_s": expected_s,
                "expected_speedup_x": baseline_s / expected_s,
                "hit_rate": hit_rate,
            }
        )
    thresholds = []
    for target in target_speedups:
        target_latency = baseline_s / target
        raw = (spatial_miss_s - target_latency) / (spatial_miss_s - cache_hit_s)
        reachable = raw <= 1.0 and target_latency >= cache_hit_s
        thresholds.append(
            {
                "reachable": reachable,
                "required_hit_rate": max(0.0, raw) if reachable else None,
                "required_hit_rate_percent": max(0.0, raw) * 100.0
                if reachable
                else None,
                "target_speedup_x": target,
            }
        )
    return {
        "empirical_integrated_route": False,
        "headline_eligible": False,
        "model_kind": "illustrative_cross_receipt_scenario_only",
        "reason": "cache and Spatial75 were measured on different fixed frames and no integrated customer router was measured",
        "scenarios": scenarios,
        "target_thresholds": thresholds,
    }


def build_model(
    *,
    observed: dict[str, Any],
    provenance: dict[str, dict[str, Any]],
    hit_rates: Iterable[float] = DEFAULT_HIT_RATES,
    shared_overheads_ms: Iterable[float] = DEFAULT_SHARED_OVERHEADS_MS,
    target_speedups: Iterable[float] = DEFAULT_TARGET_SPEEDUPS,
    link_rates_mbps: Iterable[float] = DEFAULT_LINK_RATES_MBPS,
) -> dict[str, Any]:
    hit_rates_list = _validate_rates(hit_rates, "hit_rates")
    overheads_ms = _validate_nonnegative(shared_overheads_ms, "shared_overheads_ms")
    overheads_s = [value / 1000.0 for value in overheads_ms]
    targets = _validate_positive(target_speedups, "target_speedups")
    if any(value < 1.0 for value in targets):
        raise CustomerPathModelError("target speedups must be >= 1")
    link_rates = _validate_positive(link_rates_mbps, "link_rates_mbps")

    baseline9 = observed["basic_cycles_frame9"]["local_wall_s"]
    cache_hit = observed["exact_cache_frame9"]["local_median_s"]
    baseline11 = observed["basic_cycles_frame11"]["local_wall_s"]
    spatial = observed["spatial75_two_render_frame11"]["local_median_s"]
    one_render = observed["spatial75_one_render_frame11"]["local_median_s"]
    payload_bytes = observed["exact_cache_frame9"]["artifact_bytes"]

    module_path = Path(__file__).resolve()
    test_path = module_path.with_name(f"test_{module_path.name}")
    implementation_pins: dict[str, Any] = {
        "model_module": {"path": str(module_path), "sha256": sha256_file(module_path)},
        "policy_module": {
            "path": str(Path(customer_policy.__file__).resolve()),
            "sha256": sha256_file(Path(customer_policy.__file__).resolve()),
        },
    }
    if test_path.is_file():
        implementation_pins["test_module"] = {
            "path": str(test_path),
            "sha256": sha256_file(test_path),
        }
    policy_test_path = Path(customer_policy.__file__).resolve().with_name(
        "test_customer_render_policy_v1.py"
    )
    if policy_test_path.is_file():
        implementation_pins["policy_test_module"] = {
            "path": str(policy_test_path),
            "sha256": sha256_file(policy_test_path),
        }

    exact_eligibility = observed["exact_cache_frame9"]["source_eligibility"]
    request_identity = observed["exact_cache_frame9"].get(
        "request_identity", "fixture-request-identity"
    )
    spatial_request_identity = observed["spatial75_two_render_frame11"][
        "request_identity"
    ]
    policy_evidence = {
        "exact_cache": {
            "evidence_status": "current",
            "artifact_request_identity": request_identity,
            "source_eligibility": {
                "experimental_preview": exact_eligibility["preview_only"],
                "production_ready": exact_eligibility["production_ready"],
                "artifact_verified": exact_eligibility["artifact_verified"],
                "billing_eligible": exact_eligibility["billing_eligible"],
            },
        },
        "spatial75": {
            "evidence_status": "current",
            "request_identity": spatial_request_identity,
            "fresh_two_render_gate_pass": observed[
                "spatial75_two_render_frame11"
            ]["reference_free_pair_gate_pass"],
        },
    }
    cache_miss_evidence = json.loads(json.dumps(policy_evidence))
    gate_failure_evidence = json.loads(json.dumps(cache_miss_evidence))
    gate_failure_evidence["spatial75"]["fresh_two_render_gate_pass"] = False
    policy_replays = {
        "current_exact_preview": customer_policy.decide_customer_render(
            {
                "intent": customer_policy.INTENT_EXPERIMENTAL_PREVIEW,
                "request_identity": request_identity,
            },
            policy_evidence,
        ),
        "current_final_request": customer_policy.decide_customer_render(
            {
                "intent": customer_policy.INTENT_FINAL,
                "request_identity": request_identity,
            },
            policy_evidence,
        ),
        "preview_cache_miss": customer_policy.decide_customer_render(
            {
                "intent": customer_policy.INTENT_EXPERIMENTAL_PREVIEW,
                "request_identity": spatial_request_identity,
            },
            cache_miss_evidence,
        ),
        "preview_gate_failure": customer_policy.decide_customer_render(
            {
                "intent": customer_policy.INTENT_EXPERIMENTAL_PREVIEW,
                "request_identity": spatial_request_identity,
            },
            gate_failure_evidence,
        ),
    }

    return {
        "claim_scope": "receipt-backed customer tradeoff model; not a live route, quote, SLA, authorization, or billable result",
        "current_customer_policy": {
            "animation_temporal": {
                "customer_selectable": False,
                "route": "shadow_audit_only",
            },
            "final_delivery": {
                "accelerated_lane_authorized_now": False,
                "reason": "current cache source lacks production/verification/billing eligibility and Spatial75 is experimental preview evidence",
                "route": "basic_cycles",
            },
            "interactive_preview_hypothesis": {
                "customer_enabled_now": False,
                "integrated_route_measured": False,
                "route_order": [
                    "eligible_exact_cache",
                    "fresh_two_render_spatial75_gate",
                    "basic_cycles_fallback",
                ],
            },
            "one_render": {
                "customer_selectable": False,
                "route": "shadow_audit_only",
            },
        },
        "evidence": "modeled_from_validated_local_unattested_components",
        "implementation_pins": implementation_pins,
        "kind": KIND,
        "limitations": [
            "basic Cycles denominators are one local-unattested trial each",
            "cache and Spatial75 evidence have different fixed frames and must not be pooled as an empirical workload",
            "queue, cold start, scene upload, network RTT, TLS, download, browser decode, and display are unmeasured",
            "Spatial75 quality evidence is one predeclared trial on one scene/frame",
            "no current accelerated lane has final production-ready, verified, billable eligibility",
            "modeled means and two-point quantiles are not a customer latency SLA",
        ],
        "models": {
            "exact_repeat_then_cycles_miss": _exact_repeat_scenarios(
                baseline_s=baseline9,
                hit_s=cache_hit,
                hit_rates=hit_rates_list,
                overheads_s=overheads_s,
                target_speedups=targets,
            ),
            "illustrative_cache_then_spatial_preview": _cross_receipt_hybrid_model(
                baseline_s=baseline11,
                cache_hit_s=cache_hit,
                spatial_miss_s=spatial,
                hit_rates=hit_rates_list,
                target_speedups=targets,
            ),
            "progressive_preview_then_full_cycles": _progressive_preview_model(
                baseline_s=baseline11,
                spatial_s=spatial,
                one_render_s=one_render,
                overheads_s=overheads_s,
            ),
            "payload_serialization_floor": {
                "artifact_bytes": payload_bytes,
                "excludes": "RTT, TLS, headers, queue, congestion, decode, and display",
                "rows": [
                    {
                        "link_mbps_decimal": rate,
                        "wire_seconds_at_line_rate": payload_bytes * 8.0
                        / (rate * 1_000_000.0),
                    }
                    for rate in link_rates
                ],
            },
        },
        "no_combined_empirical_speedup_claim": True,
        "observed_lanes": observed,
        "policy_replays": policy_replays,
        "schema_version": SCHEMA_VERSION,
        "source_receipts": provenance,
    }


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-receipt", required=True, type=Path)
    parser.add_argument("--spatial-receipt", required=True, type=Path)
    parser.add_argument("--one-render-report", required=True, type=Path)
    parser.add_argument("--temporal-receipt", required=True, type=Path)
    parser.add_argument(
        "--hit-rates",
        default=",".join(str(value) for value in DEFAULT_HIT_RATES),
    )
    parser.add_argument(
        "--shared-overheads-ms",
        default=",".join(str(value) for value in DEFAULT_SHARED_OVERHEADS_MS),
    )
    parser.add_argument(
        "--target-speedups",
        default=",".join(str(value) for value in DEFAULT_TARGET_SPEEDUPS),
    )
    parser.add_argument(
        "--link-rates-mbps",
        default=",".join(str(value) for value in DEFAULT_LINK_RATES_MBPS),
    )
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        receipts, provenance = load_validated_receipts(
            cache_receipt_path=args.cache_receipt,
            spatial_receipt_path=args.spatial_receipt,
            one_render_report_path=args.one_render_report,
            temporal_receipt_path=args.temporal_receipt,
        )
        observed = extract_observed_evidence(receipts)
        model = build_model(
            observed=observed,
            provenance=provenance,
            hit_rates=parse_csv_numbers(args.hit_rates, "hit_rates"),
            shared_overheads_ms=parse_csv_numbers(
                args.shared_overheads_ms, "shared_overheads_ms"
            ),
            target_speedups=parse_csv_numbers(
                args.target_speedups, "target_speedups"
            ),
            link_rates_mbps=parse_csv_numbers(
                args.link_rates_mbps, "link_rates_mbps"
            ),
        )
        sys.stdout.write(canonical_json(model, pretty=args.pretty))
        return 0
    except Exception as exc:
        sys.stderr.write(
            canonical_json(
                {
                    "error": f"{type(exc).__name__}: {exc}"[:4000],
                    "kind": KIND,
                    "ok": False,
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
