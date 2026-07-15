#!/usr/bin/env python3
"""Evaluate the strict, non-composable ComputeExchange 1,000x lane standard.

This ingests declared benchmark evidence. It does not run a benchmark, invent a
baseline, average lane multipliers, or multiply components from different
workloads. A lane only clears the standard through one direct comparison.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence

import cx_inference_receipt_v1 as inference_receipt


SCHEMA_VERSION = 1
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INFERENCE_BINDING_TOLERANCE = 0.000001

MODALITIES = {"render", "video", "inference", "image", "transcode", "project"}
METRIC_KINDS = {
    "exact_reuse",
    "fresh_render",
    "temporal",
    "inference_endpoint",
    "transcode",
    "project_turnaround",
    "capacity_only",
}
CLAIM_AXES = {
    "request_turnaround",
    "fresh_frame_latency",
    "project_turnaround",
    "sequence_turnaround",
    "transcode_turnaround",
    "inference_decode_throughput",
    "inference_request_turnaround",
}
DIRECTIONS = {"lower_is_better", "higher_is_better"}
STATISTICS = {"median", "p95", "slowest", "steady_state"}
TIMING_SCOPES = {"integrated_wall", "endpoint_e2e", "composed", "throughput_only"}
BASELINE_SOURCES = {"measured", "modeled"}
EVIDENCE_CLASSES = {
    "physical_independently_attested",
    "physical_local_unattested",
    "historical_unreplayed",
    "synthetic",
    "composed_estimate",
}
QUALITY_GATES = {
    "exact",
    "predeclared_independent",
    "posthoc",
    "parity_failed",
    "not_run",
    "not_applicable",
}
DIRECT_EVIDENCE = {"physical_independently_attested", "physical_local_unattested"}
ELIGIBLE_QUALITY = {"exact", "predeclared_independent"}
INFERENCE_REUSE_SCORECARD_LANES = {
    "inference.exact-request-reuse",
    "inference.shared-prefix-reuse",
}


class ScorecardError(ValueError):
    """A scorecard violates the lane comparison contract."""


def _duplicate_safe_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ScorecardError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path | str) -> dict[str, Any]:
    source = Path(path)
    try:
        with source.open("r", encoding="utf-8") as handle:
            value = json.load(handle, object_pairs_hook=_duplicate_safe_object)
    except (OSError, json.JSONDecodeError) as exc:
        raise ScorecardError(f"cannot read {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise ScorecardError(f"{source}: root must be an object")
    return value


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def pretty_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ScorecardError(f"{context} must be an object")
    return value


def _exact(value: Mapping[str, Any], fields: set[str], context: str) -> None:
    missing = sorted(fields - set(value))
    unknown = sorted(set(value) - fields)
    if missing:
        raise ScorecardError(f"{context} missing field(s): {', '.join(missing)}")
    if unknown:
        raise ScorecardError(f"{context} has unknown field(s): {', '.join(unknown)}")


def _string(value: Any, context: str, *, maximum: int = 1024) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScorecardError(f"{context} must be a non-empty string")
    if len(value.encode("utf-8")) > maximum:
        raise ScorecardError(f"{context} exceeds {maximum} UTF-8 bytes")
    return value


def _identifier(value: Any, context: str) -> str:
    result = _string(value, context, maximum=128)
    if not ID_RE.fullmatch(result):
        raise ScorecardError(f"{context} has invalid identifier {result!r}")
    return result


def _enum(value: Any, choices: set[str], context: str) -> str:
    result = _string(value, context, maximum=128)
    if result not in choices:
        raise ScorecardError(f"{context} must be one of: {', '.join(sorted(choices))}")
    return result


def _number(value: Any, context: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ScorecardError(f"{context} must be a finite number")
    result = float(value)
    if result < minimum:
        raise ScorecardError(f"{context} must be >= {minimum}")
    return result


def _integer(value: Any, context: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ScorecardError(f"{context} must be an integer >= {minimum}")
    return value


def _boolean(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise ScorecardError(f"{context} must be boolean")
    return value


def _relative_path(value: Any, context: str) -> str:
    result = _string(value, context, maximum=512)
    path = Path(result)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ScorecardError(f"{context} must be a relative repository path")
    return result


def _sha256_or_null(value: Any, context: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ScorecardError(f"{context} must be a lowercase SHA-256 or null")
    return value


def _unit(value: Any, context: str) -> str:
    result = _string(value, context, maximum=64)
    if any(character.isspace() for character in result):
        raise ScorecardError(f"{context} cannot contain whitespace")
    return result


def _validate_measurement(value: Any, context: str) -> dict[str, Any] | None:
    if value is None:
        return None
    row = _object(value, context)
    _exact(
        row,
        {
            "direction",
            "unit",
            "statistic",
            "baseline_value",
            "candidate_value",
            "same_logical_work",
            "timing_scope",
            "baseline_source",
            "evidence_class",
            "trials",
        },
        context,
    )
    _enum(row["direction"], DIRECTIONS, f"{context}.direction")
    _unit(row["unit"], f"{context}.unit")
    _enum(row["statistic"], STATISTICS, f"{context}.statistic")
    _number(row["baseline_value"], f"{context}.baseline_value", minimum=0.000000001)
    _number(row["candidate_value"], f"{context}.candidate_value", minimum=0.000000001)
    _boolean(row["same_logical_work"], f"{context}.same_logical_work")
    _enum(row["timing_scope"], TIMING_SCOPES, f"{context}.timing_scope")
    _enum(row["baseline_source"], BASELINE_SOURCES, f"{context}.baseline_source")
    _enum(row["evidence_class"], EVIDENCE_CLASSES, f"{context}.evidence_class")
    _integer(row["trials"], f"{context}.trials", minimum=1)
    return copy.deepcopy(row)


def _validate_authorization(value: Any, context: str) -> dict[str, bool]:
    row = _object(value, context)
    fields = {
        "artifact_verified",
        "customer_selectable",
        "publication_eligible",
        "production_ready",
        "billing_eligible",
    }
    _exact(row, fields, context)
    return {field: _boolean(row[field], f"{context}.{field}") for field in sorted(fields)}


def _validate_evidence(value: Any, context: str) -> dict[str, str | None]:
    row = _object(value, context)
    _exact(row, {"source", "receipt_sha256", "workload_sha256"}, context)
    return {
        "source": _relative_path(row["source"], f"{context}.source"),
        "receipt_sha256": _sha256_or_null(row["receipt_sha256"], f"{context}.receipt_sha256"),
        "workload_sha256": _sha256_or_null(row["workload_sha256"], f"{context}.workload_sha256"),
    }


def _validate_lift(value: Any, context: str) -> dict[str, str]:
    row = _object(value, context)
    _exact(row, {"limiter", "next_experiment"}, context)
    return {
        "limiter": _string(row["limiter"], f"{context}.limiter", maximum=500),
        "next_experiment": _string(row["next_experiment"], f"{context}.next_experiment", maximum=500),
    }


def _validate_guardrail(value: Any, context: str) -> dict[str, Any] | None:
    if value is None:
        return None
    row = _object(value, context)
    _exact(row, {"statistic", "observed_multiplier", "minimum_multiplier"}, context)
    _enum(row["statistic"], STATISTICS, f"{context}.statistic")
    observed = _number(row["observed_multiplier"], f"{context}.observed_multiplier", minimum=0.000000001)
    minimum = _number(row["minimum_multiplier"], f"{context}.minimum_multiplier", minimum=1.0)
    if observed < minimum:
        raise ScorecardError(f"{context}.observed_multiplier is below its declared minimum")
    return copy.deepcopy(row)


def _validate_coverage(value: Any, context: str) -> dict[str, Any] | None:
    if value is None:
        return None
    row = _object(value, context)
    _exact(
        row,
        {"required_hit_rate", "observed_hit_rate", "tail_multiplier", "tail_statistic"},
        context,
    )
    required = _number(row["required_hit_rate"], f"{context}.required_hit_rate", minimum=0.0)
    observed = row["observed_hit_rate"]
    if observed is not None:
        observed = _number(observed, f"{context}.observed_hit_rate", minimum=0.0)
    if required > 1.0 or (observed is not None and observed > 1.0):
        raise ScorecardError(f"{context} rates must be <= 1")
    tail = row["tail_multiplier"]
    if tail is not None:
        tail = _number(tail, f"{context}.tail_multiplier", minimum=0.000000001)
    statistic = row["tail_statistic"]
    if statistic is not None:
        _enum(statistic, STATISTICS, f"{context}.tail_statistic")
    if (tail is None) != (statistic is None):
        raise ScorecardError(f"{context}.tail_multiplier and tail_statistic must be both null or both set")
    return copy.deepcopy(row)


def _validate_lane(value: Any, context: str) -> dict[str, Any]:
    row = _object(value, context)
    fields = {
        "lane_id",
        "display_name",
        "modality",
        "metric_kind",
        "claim_axis",
        "comparison_group",
        "measurement",
        "quality_gate",
        "authorization",
        "evidence",
        "guardrail",
        "coverage",
        "lift",
    }
    _exact(row, fields, context)
    result = copy.deepcopy(row)
    result["lane_id"] = _identifier(row["lane_id"], f"{context}.lane_id")
    result["display_name"] = _string(row["display_name"], f"{context}.display_name", maximum=160)
    result["modality"] = _enum(row["modality"], MODALITIES, f"{context}.modality")
    result["metric_kind"] = _enum(row["metric_kind"], METRIC_KINDS, f"{context}.metric_kind")
    result["claim_axis"] = _enum(row["claim_axis"], CLAIM_AXES, f"{context}.claim_axis")
    result["comparison_group"] = _identifier(row["comparison_group"], f"{context}.comparison_group")
    result["measurement"] = _validate_measurement(row["measurement"], f"{context}.measurement")
    result["quality_gate"] = _enum(row["quality_gate"], QUALITY_GATES, f"{context}.quality_gate")
    result["authorization"] = _validate_authorization(row["authorization"], f"{context}.authorization")
    result["evidence"] = _validate_evidence(row["evidence"], f"{context}.evidence")
    result["guardrail"] = _validate_guardrail(row["guardrail"], f"{context}.guardrail")
    result["coverage"] = _validate_coverage(row["coverage"], f"{context}.coverage")
    result["lift"] = _validate_lift(row["lift"], f"{context}.lift")
    if result["measurement"] is None and result["quality_gate"] not in {"not_run", "not_applicable"}:
        raise ScorecardError(f"{context}.quality_gate must be not_run or not_applicable without a measurement")
    coverage_permitted = result["metric_kind"] == "exact_reuse" or (
        result["modality"] == "inference"
        and result["metric_kind"] == "inference_endpoint"
        and result["lane_id"] in INFERENCE_REUSE_SCORECARD_LANES
    )
    if result["coverage"] is not None and not coverage_permitted:
        raise ScorecardError(
            f"{context}.coverage is reserved for exact_reuse or named inference reuse lanes"
        )
    return result


def validate_scorecard(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a source scorecard and return a stable copy."""

    row = _object(value, "scorecard")
    _exact(row, {"schema_version", "standard_id", "as_of", "target_multiplier", "lanes"}, "scorecard")
    if type(row["schema_version"]) is not int or row["schema_version"] != SCHEMA_VERSION:
        raise ScorecardError(f"scorecard.schema_version must be integer {SCHEMA_VERSION}")
    standard_id = _identifier(row["standard_id"], "scorecard.standard_id")
    as_of = _string(row["as_of"], "scorecard.as_of", maximum=10)
    if not DATE_RE.fullmatch(as_of):
        raise ScorecardError("scorecard.as_of must use YYYY-MM-DD")
    target = _number(row["target_multiplier"], "scorecard.target_multiplier", minimum=1.0)
    raw_lanes = row["lanes"]
    if not isinstance(raw_lanes, list) or not raw_lanes:
        raise ScorecardError("scorecard.lanes must be a non-empty list")
    lanes = [_validate_lane(lane, f"scorecard.lanes[{index}]") for index, lane in enumerate(raw_lanes)]
    lane_ids = [str(lane["lane_id"]) for lane in lanes]
    if len(lane_ids) != len(set(lane_ids)):
        raise ScorecardError("scorecard.lanes contains duplicate lane_id")
    return {
        "schema_version": SCHEMA_VERSION,
        "standard_id": standard_id,
        "as_of": as_of,
        "target_multiplier": target,
        "lanes": lanes,
    }


def bind_inference_receipt_to_lane(
    lane_value: Mapping[str, Any],
    receipt_value: Mapping[str, Any],
    *,
    receipt_file_sha256: str,
    scorecard_target_multiplier: float,
    artifacts_verified: bool,
    receipt_source: str | None = None,
) -> dict[str, Any]:
    """Fail closed when a scorecard inference row is not the exact receipt projection.

    This is intentionally opt-in so historical/audit rows remain visible without
    being recast as new evidence.  A caller promoting a new inference row must
    provide the receipt file and verify its externally retained artifacts first.
    """

    if artifacts_verified is not True:
        raise ScorecardError(
            "inference receipt binding requires externally verified immutable artifacts"
        )
    lane = _validate_lane(lane_value, "inference scorecard lane")
    try:
        receipt = inference_receipt.validate_receipt(receipt_value)
        projection = inference_receipt.scorecard_projection(
            receipt, receipt_file_sha256=receipt_file_sha256
        )
    except inference_receipt.InferenceReceiptError as exc:
        raise ScorecardError(f"inference receipt is invalid: {exc}") from exc

    target = _number(
        scorecard_target_multiplier, "scorecard_target_multiplier", minimum=1.0
    )
    receipt_target = float(receipt["comparison"]["target_multiplier"])
    if not math.isclose(
        target, receipt_target, rel_tol=0.0, abs_tol=INFERENCE_BINDING_TOLERANCE
    ):
        raise ScorecardError(
            "inference receipt target_multiplier does not match the scorecard target"
        )
    if receipt_source is not None and lane["evidence"]["source"] != receipt_source:
        raise ScorecardError("inference receipt source does not match lane.evidence.source")

    expected_scalars = {
        "lane_id": projection["lane_id"],
        "display_name": projection["display_name"],
        "modality": projection["modality"],
        "metric_kind": projection["metric_kind"],
        "claim_axis": projection["claim_axis"],
        "comparison_group": projection["comparison_group"],
        "quality_gate": projection["quality_gate"],
    }
    for field, expected in expected_scalars.items():
        if lane[field] != expected:
            raise ScorecardError(f"inference receipt binding mismatch: lane.{field}")

    measurement = lane["measurement"]
    if measurement is None:
        raise ScorecardError("inference receipt binding requires a measured lane")
    expected_measurement = projection["measurement"]
    for field, expected in expected_measurement.items():
        actual = measurement[field]
        if isinstance(expected, float):
            if not math.isclose(
                float(actual), expected, rel_tol=0.0, abs_tol=INFERENCE_BINDING_TOLERANCE
            ):
                raise ScorecardError(f"inference receipt binding mismatch: measurement.{field}")
        elif actual != expected:
            raise ScorecardError(f"inference receipt binding mismatch: measurement.{field}")

    if lane["authorization"] != projection["authorization"]:
        raise ScorecardError("inference receipt binding mismatch: authorization")
    expected_evidence = projection["evidence"]
    for field, expected in expected_evidence.items():
        if lane["evidence"][field] != expected:
            raise ScorecardError(f"inference receipt binding mismatch: evidence.{field}")
    if lane["coverage"] != projection["coverage"]:
        raise ScorecardError("inference receipt binding mismatch: coverage")

    return {
        "lane_id": projection["lane_id"],
        "receipt_self_sha256": receipt["receipt_sha256"],
        "receipt_file_sha256": receipt_file_sha256,
        "workload_sha256": receipt["workload_sha256"],
        "trials_per_arm": projection["measurement"]["trials"],
        "p50_multiplier": projection["p50_multiplier"],
        "p95_multiplier": projection["p95_multiplier"],
        "target_multiplier": projection["target_multiplier"],
        "artifact_bindings_verified": True,
    }


def _parse_inference_receipt_specs(values: Sequence[str]) -> list[tuple[str, str]]:
    parsed: list[tuple[str, str]] = []
    lane_ids: set[str] = set()
    for raw in values:
        lane_id, separator, source = raw.partition("=")
        if not separator or not lane_id or not source:
            raise ScorecardError("--bind-inference-receipt must use LANE=RELATIVE_RECEIPT_PATH")
        lane_id = _identifier(lane_id, "--bind-inference-receipt lane")
        source = _relative_path(source, "--bind-inference-receipt receipt path")
        source = Path(source).as_posix()
        if lane_id in lane_ids:
            raise ScorecardError("--bind-inference-receipt has duplicate lane")
        lane_ids.add(lane_id)
        parsed.append((lane_id, source))
    return parsed


def _rounded(value: float) -> float:
    if not math.isfinite(value):
        raise ScorecardError("derived metric is not finite")
    return round(value, 9)


def _authorization_ready(value: Mapping[str, bool]) -> bool:
    return all(value.values())


def _coverage_status(value: Mapping[str, Any] | None, target: float) -> str | None:
    if value is None:
        return None
    observed = value["observed_hit_rate"]
    tail = value["tail_multiplier"]
    if observed is None:
        return "PORTFOLIO_NOT_EVALUABLE"
    if observed >= value["required_hit_rate"] and tail is not None and tail >= target:
        return "PORTFOLIO_TARGET_MET"
    return "PORTFOLIO_TARGET_NOT_MET"


def evaluate_lane(lane_value: Mapping[str, Any], *, target_multiplier: float) -> dict[str, Any]:
    """Compute one lane's target state without combining it with another lane."""

    lane = _validate_lane(lane_value, "lane")
    measurement = lane["measurement"]
    result: dict[str, Any] = {
        "lane_id": lane["lane_id"],
        "display_name": lane["display_name"],
        "modality": lane["modality"],
        "metric_kind": lane["metric_kind"],
        "claim_axis": lane["claim_axis"],
        "comparison_group": lane["comparison_group"],
        "quality_gate": lane["quality_gate"],
        "authorization": copy.deepcopy(lane["authorization"]),
        "evidence": copy.deepcopy(lane["evidence"]),
        "guardrail": copy.deepcopy(lane["guardrail"]),
        "coverage": copy.deepcopy(lane["coverage"]),
        "coverage_status": _coverage_status(lane["coverage"], target_multiplier),
        "lift": copy.deepcopy(lane["lift"]),
    }
    if measurement is None:
        result.update(
            {
                "measurement": None,
                "numeric_multiplier": None,
                "target_candidate_value": None,
                "candidate_distance_to_target": None,
                "remaining_multiplier_to_target": None,
                "performance_status": "unmeasured",
                "claim_status": "unmeasured",
            }
        )
        return result

    baseline = float(measurement["baseline_value"])
    candidate = float(measurement["candidate_value"])
    if measurement["direction"] == "lower_is_better":
        multiplier = baseline / candidate
        target_candidate = baseline / target_multiplier
        distance = candidate - target_candidate
    else:
        multiplier = candidate / baseline
        target_candidate = baseline * target_multiplier
        distance = target_candidate - candidate
    evidence_direct = (
        measurement["same_logical_work"]
        and measurement["timing_scope"] == "integrated_wall"
        and measurement["baseline_source"] == "measured"
        and measurement["evidence_class"] in DIRECT_EVIDENCE
        and lane["evidence"]["receipt_sha256"] is not None
    )
    quality_eligible = lane["quality_gate"] in ELIGIBLE_QUALITY
    authorization_ready = _authorization_ready(lane["authorization"])
    if lane["quality_gate"] == "parity_failed":
        performance_status = "incomparable"
        claim_status = "quarantined"
    elif not evidence_direct:
        performance_status = "incomparable"
        claim_status = (
            "historical"
            if measurement["evidence_class"] == "historical_unreplayed"
            else "audit_only"
        )
    elif not quality_eligible:
        performance_status = "above_target" if multiplier >= target_multiplier else "lift_required"
        claim_status = "audit_only"
    elif multiplier < target_multiplier:
        performance_status = "lift_required"
        claim_status = "experimental"
    elif authorization_ready:
        performance_status = "above_target"
        claim_status = "production"
    else:
        performance_status = "above_target"
        claim_status = "experimental"
    result.update(
        {
            "measurement": copy.deepcopy(measurement),
            "numeric_multiplier": _rounded(multiplier),
            "target_candidate_value": _rounded(target_candidate),
            "candidate_distance_to_target": _rounded(distance),
            "remaining_multiplier_to_target": _rounded(max(1.0, target_multiplier / multiplier)),
            "performance_status": performance_status,
            "claim_status": claim_status,
            "direct_evidence_eligible": evidence_direct,
            "quality_eligible": quality_eligible,
            "authorization_ready": authorization_ready,
        }
    )
    return result


def evaluate_scorecard(value: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate all lanes while refusing an aggregate or cross-axis claim."""

    scorecard = validate_scorecard(value)
    target = float(scorecard["target_multiplier"])
    lanes = sorted(
        (evaluate_lane(lane, target_multiplier=target) for lane in scorecard["lanes"]),
        key=lambda lane: str(lane["lane_id"]),
    )
    performance_counts: dict[str, int] = {}
    claim_counts: dict[str, int] = {}
    axes: dict[str, list[str]] = {}
    for lane in lanes:
        performance = str(lane["performance_status"])
        claim = str(lane["claim_status"])
        performance_counts[performance] = performance_counts.get(performance, 0) + 1
        claim_counts[claim] = claim_counts.get(claim, 0) + 1
        axes.setdefault(str(lane["claim_axis"]), []).append(str(lane["lane_id"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "cx_1000x_lane_scorecard",
        "standard_id": scorecard["standard_id"],
        "as_of": scorecard["as_of"],
        "target_multiplier": _rounded(target),
        "source_scorecard_sha256": sha256_json(scorecard),
        "aggregate_multiplier": None,
        "aggregate_refusal": (
            "No aggregate multiplier: separate comparison groups, claim axes, timing scopes, "
            "and quality gates cannot prove one another."
        ),
        "performance_status_counts": dict(sorted(performance_counts.items())),
        "claim_status_counts": dict(sorted(claim_counts.items())),
        "claim_axis_membership": {axis: sorted(ids) for axis, ids in sorted(axes.items())},
        "lanes": lanes,
    }


def _check_lanes(
    result: Mapping[str, Any],
    preserve: Sequence[str],
    targets: Sequence[str],
    *,
    verified_inference_receipts: set[str] | None = None,
) -> list[str]:
    rows = {str(row["lane_id"]): row for row in result["lanes"]}
    verified_inference_receipts = verified_inference_receipts or set()
    errors: list[str] = []
    for lane_id in preserve:
        lane = rows.get(lane_id)
        if lane is None:
            errors.append(f"unknown preserve lane {lane_id}")
            continue
        guardrail = lane["guardrail"]
        if guardrail is None:
            errors.append(f"preserve lane {lane_id} has no guardrail")
        elif guardrail["observed_multiplier"] < guardrail["minimum_multiplier"]:
            errors.append(f"preserve lane {lane_id} is below its guardrail")
    for lane_id in targets:
        lane = rows.get(lane_id)
        if lane is None:
            errors.append(f"unknown target lane {lane_id}")
        elif lane["performance_status"] != "above_target":
            errors.append(f"target lane {lane_id} has status {lane['performance_status']}")
        elif lane["modality"] == "inference" and lane_id not in verified_inference_receipts:
            errors.append(
                f"target inference lane {lane_id} has no immutable receipt binding; "
                "use --bind-inference-receipt"
            )
    return errors


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scorecard", type=Path, required=True, help="strict source scorecard JSON")
    parser.add_argument("--output", type=Path, help="write deterministic result JSON instead of stdout")
    parser.add_argument("--check-preserve", action="append", default=[], metavar="LANE")
    parser.add_argument("--check-target", action="append", default=[], metavar="LANE")
    parser.add_argument(
        "--bind-inference-receipt",
        action="append",
        default=[],
        metavar="LANE=RELATIVE_RECEIPT_PATH",
        help=(
            "verify a new inference row against one immutable receipt and all of its "
            "retained artifacts; no historical row is implicitly promoted"
        ),
    )
    parser.add_argument(
        "--inference-artifact-root",
        type=Path,
        help="root containing the receipt's relative immutable capture artifacts",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        scorecard = validate_scorecard(load_json(args.scorecard))
        result = evaluate_scorecard(scorecard)
        inference_receipt_specs = _parse_inference_receipt_specs(args.bind_inference_receipt)
        if inference_receipt_specs and args.inference_artifact_root is None:
            raise ScorecardError(
                "--inference-artifact-root is required with --bind-inference-receipt"
            )
        verified_inference_receipts: set[str] = set()
        if inference_receipt_specs:
            source_lanes = {str(lane["lane_id"]): lane for lane in scorecard["lanes"]}
            bindings: list[dict[str, Any]] = []
            for lane_id, source in inference_receipt_specs:
                lane = source_lanes.get(lane_id)
                if lane is None:
                    raise ScorecardError(f"unknown inference receipt lane {lane_id}")
                try:
                    receipt, raw_sha = inference_receipt.load_receipt_path(
                        Path(source),
                        artifact_root=args.inference_artifact_root,
                        verify_artifacts=True,
                    )
                except inference_receipt.InferenceReceiptError as exc:
                    raise ScorecardError(f"inference receipt is invalid: {exc}") from exc
                bindings.append(
                    bind_inference_receipt_to_lane(
                        lane,
                        receipt,
                        receipt_file_sha256=raw_sha,
                        scorecard_target_multiplier=float(scorecard["target_multiplier"]),
                        artifacts_verified=True,
                        receipt_source=source,
                    )
                )
                verified_inference_receipts.add(lane_id)
            result["inference_receipt_bindings"] = sorted(
                bindings, key=lambda binding: str(binding["lane_id"])
            )
        errors = _check_lanes(
            result,
            args.check_preserve,
            args.check_target,
            verified_inference_receipts=verified_inference_receipts,
        )
        if args.output is None:
            sys.stdout.buffer.write(pretty_json_bytes(result))
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(pretty_json_bytes(result))
        if errors:
            for error in errors:
                print(f"score-1000x-lanes: {error}", file=sys.stderr)
            return 3
    except (ScorecardError, OSError, json.JSONDecodeError) as exc:
        print(f"score-1000x-lanes: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
