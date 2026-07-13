#!/usr/bin/env python3
"""Fail-closed consistency verifier for the local Cycles transfer matrix.

The matrix is a local, unattested engineering proof.  This verifier binds each
row to the raw bytes of its referenced benchmark receipt, recomputes all claim
fields that those receipts support, and, when the receipt's local artifact root
is still present, corroborates the draft/verification manifests and PNG hashes.

The product verification manifests and most Pavilion provenance fields are not
hashed by the v1 benchmark receipts.  They are therefore reported as local
corroboration, never as cryptographically closed receipt evidence.
"""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import stat
import sys
from typing import Any
from urllib.parse import urlsplit


MATRIX_KIND = "cx_local_cycles_spec_transfer_matrix"
RECEIPT_KIND = "cx_local_cycles_spec_benchmark"
RESULT_KIND = "cx_render_transfer_matrix_verification"
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT_BYTES = 256 * 1024 * 1024

QUALITY_METRIC = "one_minus_mean_absolute_rgb_difference"
GLOBAL_MIN = 0.9
REGIONAL_MIN = 0.85
MICROTILE_MIN = 0.7
MICROTILE_EDGE = 32

EXPECTED_SCENES = {
    "classroom": "interior_many_light_gi",
    "pavilion": "archviz_sun_exterior_glass_water",
    "bmw27": "studio_glossy_specular",
}

PAVILION_URL = (
    "https://download.blender.org/demo/test/"
    "pabellon_barcelona_v1.scene_.zip"
)
PAVILION_ARCHIVE_SHA256 = (
    "0a85bf512329f1780563069512b934d679a6c543eeb1a4ec4f60cddeab706697"
)
PAVILION_ORIGINAL_SCENE_SHA256 = (
    "92162db8cabcd03641c53df95554b604b494ba74a76a099c7e06637e9379120a"
)
PAVILION_PROBE = {
    "resolution": [512, 288],
    "frame": 1,
    "samples": 24,
    "seed": 1287964443,
    "sample_offset": 0,
    "source_in_memory_sanitized_png_sha256": (
        "481ca5a59a168505a54a302890ecf82adb34fc4e1d40c428f4c5825d72dba3c1"
    ),
    "derivative_png_sha256": (
        "52f09174461d305b0f207f8f783675d385f4e0b57725a8136350642f7c6c9685"
    ),
    "global_rgb_agreement": 0.9999999734051607,
    "worst_regional_rgb_agreement": 0.9999987234477125,
    "worst_microtile_rgb_agreement": 0.9999987234477125,
}
BMW27_URL = "https://download.blender.org/demo/test/BMW27_2.blend.zip"
BMW27_ARCHIVE_SHA256 = (
    "74f5dc6d718fc565e0ff50e355be7f8b1a58983cc9ae79775608d8905c269ee4"
)
PAVILION_OPERATIONS = [
    (
        "removed unused legacy Texture.002 and its sole image reference to "
        "absent //textures/water bump.jpg"
    ),
    (
        "rewrote fourteen live unpacked image paths to bundle-local "
        "//textures basenames"
    ),
    (
        "saved a distinct Blender 4.2 derivative without replacing the "
        "official scene"
    ),
]

MATRIX_KEYS = {
    "schema_version",
    "kind",
    "date",
    "evidence",
    "receipt_trust",
    "preview_only",
    "production_ready",
    "artifact_verified",
    "claim_scope",
    "host",
    "quality_contract",
    "summary",
    "scenes",
    "limitations",
}
RECEIPT_KEYS = {
    "artifact_root",
    "baseline_s",
    "benchmark_audit",
    "benchmark_wall_s",
    "cache_used",
    "claim_scope",
    "cold_start_included",
    "controller_receipt",
    "device",
    "draft_samples",
    "evidence",
    "execution_identity_revalidation",
    "execution_order",
    "frame",
    "global_agreement",
    "host",
    "kind",
    "meets_100x_preview_experiment",
    "meets_50x_preview_experiment",
    "order_bias_control",
    "outputs",
    "pins",
    "preview_only",
    "production_ready",
    "quality_contract",
    "quality_gate",
    "quality_metric",
    "receipt_trust",
    "reference_samples",
    "reference_used_for_product_decision",
    "renderer_identity",
    "resolution",
    "sample_ranges",
    "sample_ranges_disjoint",
    "scene",
    "scene_sha256",
    "schema_version",
    "spec_s",
    "speedup_x",
    "timing_scope",
    "timing_statistics",
    "trial_count",
    "variance_estimate",
    "verify_samples",
    "warmup_candidate_runs",
    "warmup_s_uncharged",
    "worker_renderer_identity",
    "worst_tile_agreement",
}
AUDIT_KEYS = {
    "baseline",
    "binding_sha256",
    "candidate",
    "global_agreement",
    "global_min",
    "kind",
    "measurement_only",
    "metric",
    "microtile_contract",
    "microtile_count",
    "microtile_grid",
    "passed",
    "product_decision_used_reference",
    "sample_ranges",
    "sample_ranges_disjoint",
    "schema_version",
    "tile_contract",
    "tile_count",
    "tile_grid",
    "worst_microtile_agreement",
    "worst_microtile_min",
    "worst_tile_agreement",
    "worst_tile_min",
}
CONTROLLER_KEYS = {
    "accepted_fraction",
    "accepted_units",
    "artifact_verified",
    "baseline_source",
    "baseline_total_time_s",
    "branch_id",
    "claim_scope",
    "details",
    "draft_cost_s",
    "evidence",
    "exact",
    "global_ssim",
    "modality",
    "overhead_cost_s",
    "p5_ssim",
    "quality_gate",
    "quality_tier",
    "repair_cost_s",
    "repaired_fraction",
    "repaired_units",
    "schema_version",
    "speedup_vs_baseline",
    "total_product_time_s",
    "units",
    "verify_cost_s",
    "worst_tile_ssim",
}
VERIFICATION_KEYS = {
    "accepted",
    "artifact_verified",
    "billing_eligible",
    "binding_sha256",
    "draft_sample_offset",
    "draft_seed",
    "evidence",
    "failing_tile_count",
    "failing_tiles",
    "failing_tiles_truncated",
    "global_agreement",
    "global_min",
    "independent_seed",
    "kind",
    "metric",
    "microtile_contract",
    "microtile_count",
    "microtile_grid",
    "preview_only",
    "production_ready",
    "repair_plan",
    "sample_ranges_disjoint",
    "schema_version",
    "selected_manifest_path",
    "tile_contract",
    "tile_count",
    "tile_grid",
    "unit_id",
    "verify_artifact",
    "verify_sample_offset",
    "verify_seed",
    "worst_microtile_agreement",
    "worst_microtile_min",
    "worst_tile_agreement",
    "worst_tile_min",
}


class MatrixValidationError(ValueError):
    """Raised for every fail-closed validation failure."""


def _error(location: str, message: str) -> MatrixValidationError:
    return MatrixValidationError(f"{location}: {message}")


def _object(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error(location, "must be an object")
    return value


def _array(value: Any, location: str) -> list[Any]:
    if not isinstance(value, list):
        raise _error(location, "must be an array")
    return value


def _string(value: Any, location: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise _error(location, "must be a non-empty string")
    return value


def _boolean(value: Any, location: str) -> bool:
    if type(value) is not bool:
        raise _error(location, "must be a boolean")
    return value


def _integer(
    value: Any, location: str, *, minimum: int | None = None
) -> int:
    if type(value) is not int:
        raise _error(location, "must be an integer")
    if minimum is not None and value < minimum:
        raise _error(location, f"must be at least {minimum}")
    return value


def _number(
    value: Any,
    location: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(location, "must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise _error(location, "must be a finite number")
    if minimum is not None and result < minimum:
        raise _error(location, f"must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise _error(location, f"must be at most {maximum}")
    return result


def _sha256(value: Any, location: str) -> str:
    result = _string(value, location)
    if len(result) != 64 or any(ch not in "0123456789abcdef" for ch in result):
        raise _error(location, "must be 64 lowercase hexadecimal characters")
    return result


def _exact_keys(value: dict[str, Any], expected: set[str], location: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if extra:
            details.append("unexpected=" + ",".join(extra))
        raise _error(location, "field set mismatch (" + "; ".join(details) + ")")


def _equal(actual: Any, expected: Any, location: str) -> None:
    def same(left: Any, right: Any) -> bool:
        if type(left) is not type(right):
            return False
        if isinstance(left, dict):
            return set(left) == set(right) and all(
                same(left[key], right[key]) for key in left
            )
        if isinstance(left, list):
            return len(left) == len(right) and all(
                same(left_item, right_item)
                for left_item, right_item in zip(left, right, strict=True)
            )
        if isinstance(left, tuple):
            return len(left) == len(right) and all(
                same(left_item, right_item)
                for left_item, right_item in zip(left, right, strict=True)
            )
        return left == right

    if not same(actual, expected):
        raise _error(location, f"expected {expected!r}, got {actual!r}")


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MatrixValidationError(f"JSON duplicate key: {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise MatrixValidationError(f"JSON non-finite constant is forbidden: {value}")


def _reject_nonfinite_tree(value: Any, location: str = "$") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise _error(location, "contains a non-finite number")
    if isinstance(value, list):
        for index, item in enumerate(value):
            _reject_nonfinite_tree(item, f"{location}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            _reject_nonfinite_tree(item, f"{location}.{key}")


def _stable_bytes(
    path: Path, location: str, *, maximum: int = MAX_JSON_BYTES
) -> bytes:
    try:
        before_link = path.lstat()
    except OSError as exc:
        raise _error(location, f"cannot stat file: {exc}") from exc
    if stat.S_ISLNK(before_link.st_mode):
        raise _error(location, "symlinks are forbidden")
    if not stat.S_ISREG(before_link.st_mode):
        raise _error(location, "must be a regular file")
    if before_link.st_size > maximum:
        raise _error(location, f"exceeds the {maximum}-byte limit")
    try:
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
    except OSError as exc:
        raise _error(location, f"cannot read file: {exc}") from exc
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if identity_before != identity_after or len(data) != before.st_size:
        raise _error(location, "file changed while it was read")
    return data


def _strict_json_bytes(data: bytes, location: str) -> dict[str, Any]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _error(location, f"is not UTF-8 JSON: {exc}") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except MatrixValidationError as exc:
        raise _error(location, str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise _error(location, f"invalid JSON: {exc.msg}") from exc
    except (OverflowError, RecursionError) as exc:
        raise _error(location, f"JSON nesting or number is unsupported: {exc}") from exc
    try:
        _reject_nonfinite_tree(value)
    except RecursionError as exc:
        raise _error(location, "JSON nesting is too deep") from exc
    return _object(value, location)


def _read_json(path: Path, location: str) -> tuple[dict[str, Any], bytes]:
    data = _stable_bytes(path, location)
    return _strict_json_bytes(data, location), data


def _relative_path(raw: Any, location: str) -> PurePosixPath:
    value = _string(raw, location)
    if "\\" in value or "\x00" in value:
        raise _error(location, "contains a forbidden path character")
    relative = PurePosixPath(value)
    if relative.is_absolute() or value != relative.as_posix():
        raise _error(location, "must be a normalized relative POSIX path")
    if not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
        raise _error(location, "cannot contain empty, dot, or parent components")
    return relative


def _contained_file(root: Path, raw: Any, location: str) -> Path:
    relative = _relative_path(raw, location)
    try:
        canonical_root = root.resolve(strict=True)
    except OSError as exc:
        raise _error(location, f"cannot resolve root: {exc}") from exc
    if not canonical_root.is_dir():
        raise _error(location, "root is not a directory")
    current = canonical_root
    for component in relative.parts:
        current = current / component
        try:
            if current.is_symlink():
                raise _error(location, "path cannot traverse symlinks")
        except OSError as exc:
            raise _error(location, f"cannot inspect path: {exc}") from exc
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(canonical_root)
    except (OSError, ValueError) as exc:
        raise _error(location, f"path is absent or escapes its root: {exc}") from exc
    if not resolved.is_file():
        raise _error(location, "must resolve to a regular file")
    return resolved


def _range(value: Any, location: str) -> tuple[int, int]:
    row = _array(value, location)
    if len(row) != 2:
        raise _error(location, "must contain exactly [start,end]")
    start = _integer(row[0], f"{location}[0]", minimum=0)
    end = _integer(row[1], f"{location}[1]", minimum=0)
    if start >= end:
        raise _error(location, "must be a non-empty half-open range")
    return start, end


def _ranges(value: Any, location: str) -> dict[str, tuple[int, int]]:
    obj = _object(value, location)
    _exact_keys(obj, {"draft", "verify", "baseline"}, location)
    result = {key: _range(obj[key], f"{location}.{key}") for key in obj}
    items = list(result.items())
    for index, (left_name, left) in enumerate(items):
        for right_name, right in items[index + 1 :]:
            if not (left[1] <= right[0] or right[1] <= left[0]):
                raise _error(
                    location,
                    f"{left_name} and {right_name} sample ranges overlap",
                )
    return result


def _wire_speedup(baseline_s: float, product_s: float) -> float:
    return round(baseline_s / product_s, 6)


def _validate_output_descriptor(receipt: dict[str, Any], location: str) -> dict[str, Any]:
    outputs = _array(receipt["outputs"], f"{location}.outputs")
    if len(outputs) != 1:
        raise _error(f"{location}.outputs", "must contain exactly one selected draft")
    output = _object(outputs[0], f"{location}.outputs[0]")
    _exact_keys(output, {"schema_version", "kind", "manifest_path"}, f"{location}.outputs[0]")
    _equal(output["schema_version"], 1, f"{location}.outputs[0].schema_version")
    _equal(output["kind"], "cx_cycles_preview_artifact", f"{location}.outputs[0].kind")
    _relative_path(output["manifest_path"], f"{location}.outputs[0].manifest_path")
    if not output["manifest_path"].endswith("/draft-manifest.json"):
        raise _error(
            f"{location}.outputs[0].manifest_path",
            "selected output must be a draft manifest",
        )
    return output


def _validate_controller(
    receipt: dict[str, Any], baseline_s: float, product_s: float, location: str
) -> None:
    controller = _object(receipt["controller_receipt"], f"{location}.controller_receipt")
    _exact_keys(controller, CONTROLLER_KEYS, f"{location}.controller_receipt")
    expected = {
        "schema_version": 1,
        "units": 1,
        "accepted_units": 1,
        "repaired_units": 0,
        "accepted_fraction": 1.0,
        "repaired_fraction": 0.0,
        "quality_gate": True,
        "repair_cost_s": 0.0,
        "artifact_verified": False,
        "baseline_source": "measured",
        "evidence": "measured",
        "modality": "render",
        "quality_tier": "preview",
        "branch_id": "local-cycles-spec-benchmark-v1",
        "exact": False,
    }
    for key, wanted in expected.items():
        _equal(controller[key], wanted, f"{location}.controller_receipt.{key}")
    _equal(
        _number(controller["baseline_total_time_s"], f"{location}.controller_receipt.baseline_total_time_s", minimum=0.0),
        baseline_s,
        f"{location}.controller_receipt.baseline_total_time_s",
    )
    _equal(
        _number(controller["total_product_time_s"], f"{location}.controller_receipt.total_product_time_s", minimum=0.0),
        product_s,
        f"{location}.controller_receipt.total_product_time_s",
    )
    _equal(
        _number(controller["speedup_vs_baseline"], f"{location}.controller_receipt.speedup_vs_baseline", minimum=0.0),
        receipt["speedup_x"],
        f"{location}.controller_receipt.speedup_vs_baseline",
    )
    costs = sum(
        _number(controller[key], f"{location}.controller_receipt.{key}", minimum=0.0)
        for key in ("draft_cost_s", "verify_cost_s", "overhead_cost_s", "repair_cost_s")
    )
    if round(costs, 6) != product_s:
        raise _error(
            f"{location}.controller_receipt",
            "component costs do not sum to total_product_time_s at wire precision",
        )


def _validate_audit(
    receipt: dict[str, Any],
    ranges: dict[str, tuple[int, int]],
    output: dict[str, Any],
    location: str,
) -> dict[str, Any]:
    audit = _object(receipt["benchmark_audit"], f"{location}.benchmark_audit")
    _exact_keys(audit, AUDIT_KEYS, f"{location}.benchmark_audit")
    constants = {
        "schema_version": 1,
        "kind": "cx_cycles_preview_benchmark_audit",
        "measurement_only": True,
        "product_decision_used_reference": False,
        "metric": QUALITY_METRIC,
        "global_min": GLOBAL_MIN,
        "worst_tile_min": REGIONAL_MIN,
        "worst_microtile_min": MICROTILE_MIN,
        "sample_ranges_disjoint": True,
        "tile_contract": "resolution_relative_regions_not_fixed_pixel_defects",
        "microtile_contract": "fixed_scale_catastrophic_defect_sentinel",
    }
    for key, wanted in constants.items():
        _equal(audit[key], wanted, f"{location}.benchmark_audit.{key}")
    binding = _sha256(audit["binding_sha256"], f"{location}.benchmark_audit.binding_sha256")
    audit_ranges = _ranges(audit["sample_ranges"], f"{location}.benchmark_audit.sample_ranges")
    _equal(audit_ranges, ranges, f"{location}.benchmark_audit.sample_ranges")

    candidate = _object(audit["candidate"], f"{location}.benchmark_audit.candidate")
    _exact_keys(candidate, {"artifact_sha256", "manifest_path", "phase", "sample_offset"}, f"{location}.benchmark_audit.candidate")
    _equal(candidate["phase"], "draft", f"{location}.benchmark_audit.candidate.phase")
    _equal(candidate["manifest_path"], output["manifest_path"], f"{location}.benchmark_audit.candidate.manifest_path")
    _equal(candidate["sample_offset"], ranges["draft"][0], f"{location}.benchmark_audit.candidate.sample_offset")
    _sha256(candidate["artifact_sha256"], f"{location}.benchmark_audit.candidate.artifact_sha256")

    baseline = _object(audit["baseline"], f"{location}.benchmark_audit.baseline")
    _exact_keys(baseline, {"artifact_sha256", "manifest_path", "phase", "sample_offset"}, f"{location}.benchmark_audit.baseline")
    _equal(baseline["phase"], "baseline", f"{location}.benchmark_audit.baseline.phase")
    _equal(baseline["sample_offset"], ranges["baseline"][0], f"{location}.benchmark_audit.baseline.sample_offset")
    _relative_path(baseline["manifest_path"], f"{location}.benchmark_audit.baseline.manifest_path")
    _sha256(baseline["artifact_sha256"], f"{location}.benchmark_audit.baseline.artifact_sha256")

    global_score = _number(audit["global_agreement"], f"{location}.benchmark_audit.global_agreement", minimum=0.0, maximum=1.0)
    regional_score = _number(audit["worst_tile_agreement"], f"{location}.benchmark_audit.worst_tile_agreement", minimum=0.0, maximum=1.0)
    micro_score = _number(audit["worst_microtile_agreement"], f"{location}.benchmark_audit.worst_microtile_agreement", minimum=0.0, maximum=1.0)
    passed = global_score >= GLOBAL_MIN and regional_score >= REGIONAL_MIN and micro_score >= MICROTILE_MIN
    _equal(audit["passed"], passed, f"{location}.benchmark_audit.passed")
    if not passed:
        raise _error(f"{location}.benchmark_audit", "reference audit did not pass")
    _equal(receipt["global_agreement"], audit["global_agreement"], f"{location}.global_agreement")
    _equal(receipt["worst_tile_agreement"], audit["worst_tile_agreement"], f"{location}.worst_tile_agreement")
    _integer(audit["tile_count"], f"{location}.benchmark_audit.tile_count", minimum=1)
    _integer(audit["microtile_count"], f"{location}.benchmark_audit.microtile_count", minimum=1)
    tile_grid = _object(audit["tile_grid"], f"{location}.benchmark_audit.tile_grid")
    micro_grid = _object(audit["microtile_grid"], f"{location}.benchmark_audit.microtile_grid")
    _equal(micro_grid.get("nominal_edge_pixels"), MICROTILE_EDGE, f"{location}.benchmark_audit.microtile_grid.nominal_edge_pixels")
    return {
        "binding": binding,
        "global": global_score,
        "regional": regional_score,
        "micro": micro_score,
        "tile_grid": tile_grid,
        "micro_grid": micro_grid,
        "candidate_sha256": candidate["artifact_sha256"],
    }


def _validate_receipt(
    receipt: dict[str, Any], row: dict[str, Any], location: str
) -> dict[str, Any]:
    _exact_keys(receipt, RECEIPT_KEYS, location)
    constants = {
        "schema_version": 1,
        "kind": RECEIPT_KIND,
        "evidence": "measured",
        "receipt_trust": "local_unattested",
        "preview_only": True,
        "production_ready": False,
        "cache_used": False,
        "cold_start_included": False,
        "trial_count": 1,
        "variance_estimate": None,
        "warmup_candidate_runs": 1,
        "sample_ranges_disjoint": True,
        "quality_gate": True,
        "quality_metric": QUALITY_METRIC,
        "reference_used_for_product_decision": False,
        "device": "GPU/METAL",
        "timing_scope": "resident_steady_state_after_uncharged_warmup",
        "timing_statistics": "fixed-order single trial; no variance estimate",
    }
    for key, wanted in constants.items():
        _equal(receipt[key], wanted, f"{location}.{key}")
    _equal(
        receipt["execution_order"],
        [
            "uncharged_full_candidate_warmup",
            "measured_baseline",
            "measured_candidate",
            "measurement_only_baseline_audit",
        ],
        f"{location}.execution_order",
    )
    _equal(
        receipt["order_bias_control"],
        "full candidate path warmed before baseline",
        f"{location}.order_bias_control",
    )
    _string(receipt["claim_scope"], f"{location}.claim_scope")
    _string(receipt["quality_contract"], f"{location}.quality_contract")
    _number(receipt["benchmark_wall_s"], f"{location}.benchmark_wall_s", minimum=0.0)
    _number(receipt["warmup_s_uncharged"], f"{location}.warmup_s_uncharged", minimum=0.0)
    _string(receipt["execution_identity_revalidation"], f"{location}.execution_identity_revalidation")

    frame = _integer(receipt["frame"], f"{location}.frame", minimum=0)
    resolution = _array(receipt["resolution"], f"{location}.resolution")
    if len(resolution) != 2:
        raise _error(f"{location}.resolution", "must contain width and height")
    width = _integer(resolution[0], f"{location}.resolution[0]", minimum=1)
    height = _integer(resolution[1], f"{location}.resolution[1]", minimum=1)
    draft_samples = _integer(receipt["draft_samples"], f"{location}.draft_samples", minimum=1)
    verify_samples = _integer(receipt["verify_samples"], f"{location}.verify_samples", minimum=1)
    reference_samples = _integer(receipt["reference_samples"], f"{location}.reference_samples", minimum=1)
    _equal([width, height], [1920, 1080], f"{location}.resolution")
    _equal(frame, 1, f"{location}.frame")
    _equal(reference_samples, 4096, f"{location}.reference_samples")
    ranges = _ranges(receipt["sample_ranges"], f"{location}.sample_ranges")
    expected_ranges = {
        "draft": (0, draft_samples),
        "verify": (draft_samples, draft_samples + verify_samples),
        "baseline": (
            draft_samples + verify_samples,
            draft_samples + verify_samples + reference_samples,
        ),
    }
    _equal(ranges, expected_ranges, f"{location}.sample_ranges")

    baseline_s = _number(receipt["baseline_s"], f"{location}.baseline_s", minimum=0.000001)
    product_s = _number(receipt["spec_s"], f"{location}.spec_s", minimum=0.000001)
    speedup = _number(receipt["speedup_x"], f"{location}.speedup_x", minimum=0.0)
    _equal(speedup, _wire_speedup(baseline_s, product_s), f"{location}.speedup_x")
    output = _validate_output_descriptor(receipt, location)
    _validate_controller(receipt, baseline_s, product_s, location)
    audit = _validate_audit(receipt, ranges, output, location)

    meets_50 = speedup >= 50.0
    meets_100 = speedup >= 100.0
    _equal(receipt["meets_50x_preview_experiment"], meets_50, f"{location}.meets_50x_preview_experiment")
    _equal(receipt["meets_100x_preview_experiment"], meets_100, f"{location}.meets_100x_preview_experiment")

    host = _object(receipt["host"], f"{location}.host")
    _exact_keys(host, {"cpu_brand", "machine", "memory_bytes", "platform", "python"}, f"{location}.host")
    for key in ("cpu_brand", "machine", "memory_bytes", "platform", "python"):
        _string(host[key], f"{location}.host.{key}")
    try:
        memory_bytes = int(host["memory_bytes"], 10)
    except ValueError as exc:
        raise _error(f"{location}.host.memory_bytes", "must be a decimal integer string") from exc
    if memory_bytes <= 0:
        raise _error(f"{location}.host.memory_bytes", "must be positive")

    pins = _object(receipt["pins"], f"{location}.pins")
    _exact_keys(
        pins,
        {
            "backend_sha256",
            "benchmark_harness_sha256",
            "blender_sha256",
            "controller_adapter_sha256",
            "controller_core_sha256",
            "render_preview_driver_sha256",
        },
        f"{location}.pins",
    )
    for key, value in pins.items():
        _sha256(value, f"{location}.pins.{key}")

    renderer = _object(receipt["renderer_identity"], f"{location}.renderer_identity")
    _string(renderer.get("version"), f"{location}.renderer_identity.version")
    runtime_bundle = _object(renderer.get("runtime_bundle"), f"{location}.renderer_identity.runtime_bundle")
    _sha256(runtime_bundle.get("sha256"), f"{location}.renderer_identity.runtime_bundle.sha256")

    worker = _object(receipt["worker_renderer_identity"], f"{location}.worker_renderer_identity")
    _exact_keys(
        worker,
        {
            "blender_build_hash",
            "blender_version",
            "dependency_count",
            "dependency_paths_sha256",
            "device",
            "enabled_device_names",
        },
        f"{location}.worker_renderer_identity",
    )
    _string(worker["blender_build_hash"], f"{location}.worker_renderer_identity.blender_build_hash")
    _string(worker["blender_version"], f"{location}.worker_renderer_identity.blender_version")
    _integer(worker["dependency_count"], f"{location}.worker_renderer_identity.dependency_count", minimum=0)
    _sha256(worker["dependency_paths_sha256"], f"{location}.worker_renderer_identity.dependency_paths_sha256")
    _equal(worker["device"], receipt["device"], f"{location}.worker_renderer_identity.device")
    names = _array(worker["enabled_device_names"], f"{location}.worker_renderer_identity.enabled_device_names")
    if len(names) != 1:
        raise _error(f"{location}.worker_renderer_identity.enabled_device_names", "must contain exactly one enabled GPU")
    _string(names[0], f"{location}.worker_renderer_identity.enabled_device_names[0]")
    renderer_version = renderer["version"]
    if renderer_version not in (
        worker["blender_version"],
        f"Blender {worker['blender_version']}",
    ):
        raise _error(
            f"{location}.worker_renderer_identity.blender_version",
            "does not agree with the operator renderer version",
        )

    scene_sha = _sha256(receipt["scene_sha256"], f"{location}.scene_sha256")
    scene_path = Path(_string(receipt["scene"], f"{location}.scene"))
    if not scene_path.is_absolute():
        raise _error(f"{location}.scene", "must be absolute")

    return {
        "receipt": receipt,
        "frame": frame,
        "resolution": [width, height],
        "draft_samples": draft_samples,
        "verify_samples": verify_samples,
        "reference_samples": reference_samples,
        "ranges": ranges,
        "baseline_s": baseline_s,
        "product_s": product_s,
        "speedup": speedup,
        "meets_50": meets_50,
        "meets_100": meets_100,
        "host": host,
        "memory_bytes": memory_bytes,
        "pins": pins,
        "renderer": renderer,
        "worker": worker,
        "worker_common": {
            key: worker[key]
            for key in (
                "blender_build_hash",
                "blender_version",
                "device",
                "enabled_device_names",
            )
        },
        "scene_sha": scene_sha,
        "scene_path": scene_path,
        "output": output,
        "audit": audit,
    }


def _validate_bundle(value: Any, location: str) -> dict[str, Any]:
    bundle = _object(value, location)
    _exact_keys(bundle, {"files", "bytes", "sha256"}, location)
    _integer(bundle["files"], f"{location}.files", minimum=1)
    _integer(bundle["bytes"], f"{location}.bytes", minimum=1)
    _sha256(bundle["sha256"], f"{location}.sha256")
    return bundle


def _validate_product_gate(value: Any, location: str) -> dict[str, Any]:
    gate = _object(value, location)
    _exact_keys(
        gate,
        {
            "accepted",
            "global_agreement",
            "worst_regional_agreement",
            "worst_microtile_agreement",
        },
        location,
    )
    global_score = _number(gate["global_agreement"], f"{location}.global_agreement", minimum=0.0, maximum=1.0)
    regional = _number(gate["worst_regional_agreement"], f"{location}.worst_regional_agreement", minimum=0.0, maximum=1.0)
    micro = _number(gate["worst_microtile_agreement"], f"{location}.worst_microtile_agreement", minimum=0.0, maximum=1.0)
    accepted = global_score >= GLOBAL_MIN and regional >= REGIONAL_MIN and micro >= MICROTILE_MIN
    _equal(gate["accepted"], accepted, f"{location}.accepted")
    return gate


def _validate_reference_audit(value: Any, facts: dict[str, Any], location: str) -> None:
    row = _object(value, location)
    _exact_keys(
        row,
        {
            "passed",
            "global_agreement",
            "worst_regional_agreement",
            "worst_microtile_agreement",
        },
        location,
    )
    expected = {
        "passed": True,
        "global_agreement": facts["audit"]["global"],
        "worst_regional_agreement": facts["audit"]["regional"],
        "worst_microtile_agreement": facts["audit"]["micro"],
    }
    _equal(row, expected, location)


def _validate_row(row: dict[str, Any], facts: dict[str, Any], location: str) -> dict[str, Any]:
    key = _string(row.get("key"), f"{location}.key")
    if key not in EXPECTED_SCENES:
        raise _error(f"{location}.key", f"unsupported scene key {key!r}")
    common = {
        "key",
        "family",
        "frame",
        "resolution",
        "draft_samples",
        "verify_samples",
        "reference_samples",
        "sample_ranges",
        "product_gate",
        "reference_audit",
        "baseline_s",
        "product_s",
        "speedup_x",
        "meets_50x_preview_experiment",
        "meets_100x_preview_experiment",
        "receipt",
    }
    extras = {
        "classroom": {"source_scene_sha256", "bundle"},
        "pavilion": {"source", "sanitized_derivative"},
        "bmw27": {"source", "bundle", "quality_limited_note"},
    }[key]
    _exact_keys(row, common | extras, location)
    _equal(row["family"], EXPECTED_SCENES[key], f"{location}.family")
    for field in ("frame", "resolution", "draft_samples", "verify_samples", "reference_samples"):
        _equal(row[field], facts[field], f"{location}.{field}")
    matrix_ranges = _object(row["sample_ranges"], f"{location}.sample_ranges")
    _exact_keys(matrix_ranges, {"draft", "verify", "reference"}, f"{location}.sample_ranges")
    expected_ranges = {
        "draft": list(facts["ranges"]["draft"]),
        "verify": list(facts["ranges"]["verify"]),
        "reference": list(facts["ranges"]["baseline"]),
    }
    _equal(matrix_ranges, expected_ranges, f"{location}.sample_ranges")
    gate = _validate_product_gate(row["product_gate"], f"{location}.product_gate")
    _equal(gate["accepted"], True, f"{location}.product_gate.accepted")
    _validate_reference_audit(row["reference_audit"], facts, f"{location}.reference_audit")
    expected_scalars = {
        "baseline_s": facts["baseline_s"],
        "product_s": facts["product_s"],
        "speedup_x": facts["speedup"],
        "meets_50x_preview_experiment": facts["meets_50"],
        "meets_100x_preview_experiment": facts["meets_100"],
    }
    for field, wanted in expected_scalars.items():
        _equal(row[field], wanted, f"{location}.{field}")

    if key == "classroom":
        _equal(row["source_scene_sha256"], facts["scene_sha"], f"{location}.source_scene_sha256")
        bundle = _validate_bundle(row["bundle"], f"{location}.bundle")
    elif key == "bmw27":
        source = _object(row["source"], f"{location}.source")
        _exact_keys(source, {"url", "archive_sha256", "scene_sha256", "official_scene_unchanged"}, f"{location}.source")
        _equal(source["url"], BMW27_URL, f"{location}.source.url")
        archive_sha = _sha256(
            source["archive_sha256"], f"{location}.source.archive_sha256"
        )
        _equal(
            archive_sha,
            BMW27_ARCHIVE_SHA256,
            f"{location}.source.archive_sha256",
        )
        _equal(source["scene_sha256"], facts["scene_sha"], f"{location}.source.scene_sha256")
        _equal(source["official_scene_unchanged"], True, f"{location}.source.official_scene_unchanged")
        bundle = _validate_bundle(row["bundle"], f"{location}.bundle")
        note = _string(row["quality_limited_note"], f"{location}.quality_limited_note")
        for fragment in ("24+24", "0.828513163", "32+32", "4096-sample audit"):
            if fragment not in note:
                raise _error(f"{location}.quality_limited_note", f"missing required fragment {fragment!r}")
    else:
        source = _object(row["source"], f"{location}.source")
        _exact_keys(source, {"url", "archive_sha256", "original_scene_sha256", "official_scene_unchanged"}, f"{location}.source")
        _equal(source["url"], PAVILION_URL, f"{location}.source.url")
        parsed = urlsplit(source["url"])
        if parsed.scheme != "https" or parsed.hostname != "download.blender.org" or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise _error(f"{location}.source.url", "must be the fixed HTTPS Blender download URL")
        _sha256(source["archive_sha256"], f"{location}.source.archive_sha256")
        _equal(
            source["archive_sha256"],
            PAVILION_ARCHIVE_SHA256,
            f"{location}.source.archive_sha256",
        )
        original_sha = _sha256(source["original_scene_sha256"], f"{location}.source.original_scene_sha256")
        _equal(
            original_sha,
            PAVILION_ORIGINAL_SCENE_SHA256,
            f"{location}.source.original_scene_sha256",
        )
        _equal(source["official_scene_unchanged"], True, f"{location}.source.official_scene_unchanged")

        derivative = _object(row["sanitized_derivative"], f"{location}.sanitized_derivative")
        _exact_keys(derivative, {"scene_sha256", "operations", "bundle", "same_seed_render_equivalence_probe"}, f"{location}.sanitized_derivative")
        _equal(derivative["scene_sha256"], facts["scene_sha"], f"{location}.sanitized_derivative.scene_sha256")
        if derivative["scene_sha256"] == original_sha:
            raise _error(f"{location}.sanitized_derivative.scene_sha256", "derivative must be distinct from original scene")
        _equal(derivative["operations"], PAVILION_OPERATIONS, f"{location}.sanitized_derivative.operations")
        bundle = _validate_bundle(derivative["bundle"], f"{location}.sanitized_derivative.bundle")
        probe = _object(derivative["same_seed_render_equivalence_probe"], f"{location}.sanitized_derivative.same_seed_render_equivalence_probe")
        _exact_keys(
            probe,
            {
                "resolution",
                "frame",
                "samples",
                "seed",
                "sample_offset",
                "source_in_memory_sanitized_png_sha256",
                "derivative_png_sha256",
                "global_rgb_agreement",
                "worst_regional_rgb_agreement",
                "worst_microtile_rgb_agreement",
            },
            f"{location}.sanitized_derivative.same_seed_render_equivalence_probe",
        )
        _equal(probe["resolution"], [512, 288], f"{location}.sanitized_derivative.same_seed_render_equivalence_probe.resolution")
        _equal(probe["frame"], 1, f"{location}.sanitized_derivative.same_seed_render_equivalence_probe.frame")
        _equal(probe["samples"], 24, f"{location}.sanitized_derivative.same_seed_render_equivalence_probe.samples")
        _integer(probe["seed"], f"{location}.sanitized_derivative.same_seed_render_equivalence_probe.seed", minimum=0)
        _equal(probe["sample_offset"], 0, f"{location}.sanitized_derivative.same_seed_render_equivalence_probe.sample_offset")
        source_png = _sha256(probe["source_in_memory_sanitized_png_sha256"], f"{location}.sanitized_derivative.same_seed_render_equivalence_probe.source_in_memory_sanitized_png_sha256")
        derivative_png = _sha256(probe["derivative_png_sha256"], f"{location}.sanitized_derivative.same_seed_render_equivalence_probe.derivative_png_sha256")
        if source_png == derivative_png:
            raise _error(f"{location}.sanitized_derivative.same_seed_render_equivalence_probe", "probe artifacts must be independently hashed images")
        for metric in (
            "global_rgb_agreement",
            "worst_regional_rgb_agreement",
            "worst_microtile_rgb_agreement",
        ):
            _number(probe[metric], f"{location}.sanitized_derivative.same_seed_render_equivalence_probe.{metric}", minimum=0.0, maximum=1.0)
        _equal(
            probe,
            PAVILION_PROBE,
            f"{location}.sanitized_derivative.same_seed_render_equivalence_probe",
        )
    return {"key": key, "gate": gate, "bundle": bundle}


def _validate_local_product(
    facts: dict[str, Any], row: dict[str, Any], bundle: dict[str, Any], location: str
) -> dict[str, Any]:
    root = Path(_string(facts["receipt"]["artifact_root"], f"{location}.artifact_root"))
    if not root.is_absolute():
        raise _error(f"{location}.artifact_root", "must be absolute")
    if not root.exists():
        return {"available": False, "checked": False, "receipt_bound": False}
    try:
        if root.is_symlink() or not root.is_dir():
            raise _error(f"{location}.artifact_root", "existing root must be a non-symlink directory")
    except OSError as exc:
        raise _error(f"{location}.artifact_root", f"cannot inspect root: {exc}") from exc

    output_path = facts["output"]["manifest_path"]
    draft_path = _contained_file(root, output_path, f"{location}.local.draft_manifest")
    draft, _ = _read_json(draft_path, f"{location}.local.draft_manifest")
    required_draft = {
        "schema_version",
        "kind",
        "phase",
        "binding_sha256",
        "artifact",
        "render",
        "scene",
        "pins",
        "preview_only",
        "production_ready",
        "artifact_verified",
        "billing_eligible",
        "evidence",
        "execution_identity_revalidation",
        "unit_id",
    }
    _exact_keys(draft, required_draft, f"{location}.local.draft_manifest")
    _equal(draft["schema_version"], 1, f"{location}.local.draft_manifest.schema_version")
    _equal(draft["kind"], "cx_cycles_preview_manifest", f"{location}.local.draft_manifest.kind")
    _equal(draft["phase"], "draft", f"{location}.local.draft_manifest.phase")
    _equal(draft["binding_sha256"], facts["audit"]["binding"], f"{location}.local.draft_manifest.binding_sha256")
    for key, wanted in {
        "preview_only": True,
        "production_ready": False,
        "artifact_verified": False,
        "billing_eligible": False,
    }.items():
        _equal(draft[key], wanted, f"{location}.local.draft_manifest.{key}")

    artifact = _object(draft["artifact"], f"{location}.local.draft_manifest.artifact")
    _exact_keys(artifact, {"path", "sha256", "media_type"}, f"{location}.local.draft_manifest.artifact")
    _equal(artifact["media_type"], "image/png", f"{location}.local.draft_manifest.artifact.media_type")
    _equal(artifact["sha256"], facts["audit"]["candidate_sha256"], f"{location}.local.draft_manifest.artifact.sha256")
    draft_png = _contained_file(root, artifact["path"], f"{location}.local.draft_artifact")
    draft_png_bytes = _stable_bytes(draft_png, f"{location}.local.draft_artifact", maximum=MAX_ARTIFACT_BYTES)
    _equal(hashlib.sha256(draft_png_bytes).hexdigest(), artifact["sha256"], f"{location}.local.draft_artifact.sha256")

    render = _object(draft["render"], f"{location}.local.draft_manifest.render")
    expected_render = {
        "width": facts["resolution"][0],
        "height": facts["resolution"][1],
        "frame": facts["frame"],
        "samples": facts["draft_samples"],
        "sample_offset": facts["ranges"]["draft"][0],
        "device": facts["receipt"]["device"],
        "engine": "CYCLES",
    }
    for key, wanted in expected_render.items():
        _equal(render.get(key), wanted, f"{location}.local.draft_manifest.render.{key}")
    _equal(render.get("worker_renderer_identity"), facts["worker"], f"{location}.local.draft_manifest.render.worker_renderer_identity")
    _integer(render.get("seed"), f"{location}.local.draft_manifest.render.seed", minimum=0)

    manifest_scene = _object(draft["scene"], f"{location}.local.draft_manifest.scene")
    _equal(manifest_scene.get("sha256"), facts["scene_sha"], f"{location}.local.draft_manifest.scene.sha256")
    expected_bundle = {
        "bundle_files": bundle["files"],
        "bundle_bytes": bundle["bytes"],
        "bundle_sha256": bundle["sha256"],
    }
    for key, wanted in expected_bundle.items():
        _equal(manifest_scene.get(key), wanted, f"{location}.local.draft_manifest.scene.{key}")
    manifest_pins = _object(draft["pins"], f"{location}.local.draft_manifest.pins")
    for key in (
        "backend_sha256",
        "blender_sha256",
        "controller_adapter_sha256",
        "controller_core_sha256",
    ):
        _equal(manifest_pins.get(key), facts["pins"][key], f"{location}.local.draft_manifest.pins.{key}")
    _sha256(manifest_pins.get("child_script_sha256"), f"{location}.local.draft_manifest.pins.child_script_sha256")

    verification_path = draft_path.parent / "verification-manifest.json"
    verification, _ = _read_json(verification_path, f"{location}.local.verification_manifest")
    _exact_keys(verification, VERIFICATION_KEYS, f"{location}.local.verification_manifest")
    constants = {
        "schema_version": 1,
        "kind": "cx_cycles_preview_verification",
        "accepted": True,
        "preview_only": True,
        "production_ready": False,
        "artifact_verified": False,
        "billing_eligible": False,
        "independent_seed": True,
        "sample_ranges_disjoint": True,
        "metric": QUALITY_METRIC,
        "global_min": GLOBAL_MIN,
        "worst_tile_min": REGIONAL_MIN,
        "worst_microtile_min": MICROTILE_MIN,
        "repair_plan": None,
        "failing_tile_count": 0,
        "failing_tiles": [],
        "failing_tiles_truncated": False,
    }
    for key, wanted in constants.items():
        _equal(verification[key], wanted, f"{location}.local.verification_manifest.{key}")
    _equal(verification["binding_sha256"], facts["audit"]["binding"], f"{location}.local.verification_manifest.binding_sha256")
    _equal(verification["selected_manifest_path"], output_path, f"{location}.local.verification_manifest.selected_manifest_path")
    _equal(verification["draft_sample_offset"], facts["ranges"]["draft"][0], f"{location}.local.verification_manifest.draft_sample_offset")
    _equal(verification["verify_sample_offset"], facts["ranges"]["verify"][0], f"{location}.local.verification_manifest.verify_sample_offset")
    draft_seed = _integer(verification["draft_seed"], f"{location}.local.verification_manifest.draft_seed", minimum=0)
    verify_seed = _integer(verification["verify_seed"], f"{location}.local.verification_manifest.verify_seed", minimum=0)
    _equal(draft_seed, render["seed"], f"{location}.local.verification_manifest.draft_seed")
    if draft_seed == verify_seed:
        raise _error(f"{location}.local.verification_manifest.verify_seed", "must be independent from draft seed")

    gate = row["product_gate"]
    product_values = {
        "global_agreement": gate["global_agreement"],
        "worst_tile_agreement": gate["worst_regional_agreement"],
        "worst_microtile_agreement": gate["worst_microtile_agreement"],
    }
    for key, wanted in product_values.items():
        _equal(verification[key], wanted, f"{location}.local.verification_manifest.{key}")
    _equal(verification["tile_grid"], facts["audit"]["tile_grid"], f"{location}.local.verification_manifest.tile_grid")
    _equal(verification["microtile_grid"], facts["audit"]["micro_grid"], f"{location}.local.verification_manifest.microtile_grid")

    verify_artifact = _object(verification["verify_artifact"], f"{location}.local.verification_manifest.verify_artifact")
    _exact_keys(verify_artifact, {"path", "sha256"}, f"{location}.local.verification_manifest.verify_artifact")
    verify_sha = _sha256(verify_artifact["sha256"], f"{location}.local.verification_manifest.verify_artifact.sha256")
    verify_png = _contained_file(root, verify_artifact["path"], f"{location}.local.verify_artifact")
    verify_png_bytes = _stable_bytes(verify_png, f"{location}.local.verify_artifact", maximum=MAX_ARTIFACT_BYTES)
    _equal(hashlib.sha256(verify_png_bytes).hexdigest(), verify_sha, f"{location}.local.verify_artifact.sha256")
    return {
        "available": True,
        "checked": True,
        "receipt_bound": False,
        "selected_draft_artifact_receipt_bound": True,
    }


def _validate_matrix_contract(matrix: dict[str, Any]) -> None:
    _exact_keys(matrix, MATRIX_KEYS, "matrix")
    constants = {
        "schema_version": 1,
        "kind": MATRIX_KIND,
        "evidence": "measured_receipt_aggregation",
        "receipt_trust": "local_unattested",
        "preview_only": True,
        "production_ready": False,
        "artifact_verified": False,
    }
    for key, wanted in constants.items():
        _equal(matrix[key], wanted, f"matrix.{key}")
    raw_date = _string(matrix["date"], "matrix.date")
    try:
        date.fromisoformat(raw_date)
    except ValueError as exc:
        raise _error("matrix.date", "must be an ISO calendar date") from exc
    claim_scope = _string(matrix["claim_scope"], "matrix.claim_scope")
    for fragment in ("three 1920x1080", "Apple M3 Ultra", "one fixed-order trial", "cold start excluded", "no variance estimate"):
        if fragment not in claim_scope:
            raise _error("matrix.claim_scope", f"missing required limitation {fragment!r}")
    limitations = _array(matrix["limitations"], "matrix.limitations")
    if len(limitations) < 6:
        raise _error("matrix.limitations", "must retain all six scoped limitations")
    for index, limitation in enumerate(limitations):
        _string(limitation, f"matrix.limitations[{index}]")

    contract = _object(matrix["quality_contract"], "matrix.quality_contract")
    _exact_keys(
        contract,
        {
            "metric",
            "global_min",
            "worst_regional_min",
            "worst_microtile_min",
            "microtile_nominal_edge_pixels",
            "reference_used_for_product_decision",
            "accepted_draft_and_disjoint_sample_ranges_required_for_50x",
        },
        "matrix.quality_contract",
    )
    expected_contract = {
        "metric": QUALITY_METRIC,
        "global_min": GLOBAL_MIN,
        "worst_regional_min": REGIONAL_MIN,
        "worst_microtile_min": MICROTILE_MIN,
        "microtile_nominal_edge_pixels": MICROTILE_EDGE,
        "reference_used_for_product_decision": False,
        "accepted_draft_and_disjoint_sample_ranges_required_for_50x": True,
    }
    _equal(contract, expected_contract, "matrix.quality_contract")


def _validate_summary(matrix: dict[str, Any], facts: list[dict[str, Any]]) -> None:
    summary = _object(matrix["summary"], "matrix.summary")
    _exact_keys(
        summary,
        {
            "scenes",
            "scenes_meeting_50x_preview_experiment",
            "scenes_meeting_100x_preview_experiment",
            "minimum_measured_speedup_x",
            "maximum_measured_speedup_x",
            "all_selected_drafts_passed_product_gate",
            "all_selected_drafts_passed_4096_sample_audit",
        },
        "matrix.summary",
    )
    expected = {
        "scenes": len(facts),
        "scenes_meeting_50x_preview_experiment": sum(item["meets_50"] for item in facts),
        "scenes_meeting_100x_preview_experiment": sum(item["meets_100"] for item in facts),
        "minimum_measured_speedup_x": min(item["speedup"] for item in facts),
        "maximum_measured_speedup_x": max(item["speedup"] for item in facts),
        "all_selected_drafts_passed_product_gate": all(item["gate"]["accepted"] for item in facts),
        "all_selected_drafts_passed_4096_sample_audit": all(item["audit"]["global"] >= GLOBAL_MIN and item["audit"]["regional"] >= REGIONAL_MIN and item["audit"]["micro"] >= MICROTILE_MIN for item in facts),
    }
    _equal(summary, expected, "matrix.summary")


def verify_transfer_matrix(
    matrix_path: Path, *, receipts_root: Path | None = None
) -> dict[str, Any]:
    """Validate one matrix and return a machine-readable success envelope."""
    matrix_path = matrix_path.expanduser()
    matrix, matrix_bytes = _read_json(matrix_path, "matrix")
    _validate_matrix_contract(matrix)
    root = (receipts_root.expanduser() if receipts_root is not None else matrix_path.parent)
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise _error("receipts_root", f"cannot resolve directory: {exc}") from exc
    if not root.is_dir():
        raise _error("receipts_root", "must be a directory")

    rows = _array(matrix["scenes"], "matrix.scenes")
    if len(rows) != len(EXPECTED_SCENES):
        raise _error("matrix.scenes", "must contain exactly classroom, pavilion, and bmw27")
    seen: set[str] = set()
    facts_rows: list[dict[str, Any]] = []
    local_results: dict[str, dict[str, Any]] = {}
    receipt_paths: set[Path] = set()
    for index, raw_row in enumerate(rows):
        row_location = f"matrix.scenes[{index}]"
        row = _object(raw_row, row_location)
        key = _string(row.get("key"), f"{row_location}.key")
        if key in seen:
            raise _error(f"{row_location}.key", f"duplicate scene key {key!r}")
        seen.add(key)
        descriptor = _object(row.get("receipt"), f"{row_location}.receipt")
        _exact_keys(descriptor, {"path", "sha256"}, f"{row_location}.receipt")
        receipt_path = _contained_file(root, descriptor["path"], f"{row_location}.receipt.path")
        if receipt_path in receipt_paths:
            raise _error(f"{row_location}.receipt.path", "receipt path is reused by another scene")
        receipt_paths.add(receipt_path)
        receipt, receipt_bytes = _read_json(receipt_path, f"receipt[{key}]")
        declared_sha = _sha256(descriptor["sha256"], f"{row_location}.receipt.sha256")
        actual_sha = hashlib.sha256(receipt_bytes).hexdigest()
        _equal(actual_sha, declared_sha, f"{row_location}.receipt.sha256")
        facts = _validate_receipt(receipt, row, f"receipt[{key}]")
        row_facts = _validate_row(row, facts, row_location)
        facts.update(row_facts)
        local_results[key] = _validate_local_product(
            facts, row, row_facts["bundle"], f"receipt[{key}]"
        )
        facts_rows.append(facts)

    _equal(seen, set(EXPECTED_SCENES), "matrix.scenes")
    first = facts_rows[0]
    for facts in facts_rows[1:]:
        for field in ("host", "pins", "renderer", "worker_common"):
            _equal(facts[field], first[field], f"shared_execution.{field}")
        _equal(
            facts["receipt"]["execution_identity_revalidation"],
            first["receipt"]["execution_identity_revalidation"],
            "shared_execution.execution_identity_revalidation",
        )

    host = _object(matrix["host"], "matrix.host")
    _exact_keys(host, {"cpu", "memory_bytes", "renderer", "device", "enabled_device_name"}, "matrix.host")
    expected_host = {
        "cpu": first["host"]["cpu_brand"],
        "memory_bytes": first["memory_bytes"],
        "renderer": f"{first['renderer']['version']} / Cycles",
        "device": first["receipt"]["device"],
        "enabled_device_name": first["worker"]["enabled_device_names"][0],
    }
    _equal(host, expected_host, "matrix.host")
    _validate_summary(matrix, facts_rows)

    checked = sum(result["checked"] for result in local_results.values())
    pavilion = next(item for item in facts_rows if item["key"] == "pavilion")
    pavilion_derivative_checked = False
    if pavilion["scene_path"].exists():
        scene_bytes = _stable_bytes(
            pavilion["scene_path"],
            "pavilion.local_derivative_scene",
            maximum=MAX_ARTIFACT_BYTES,
        )
        _equal(
            hashlib.sha256(scene_bytes).hexdigest(),
            pavilion["scene_sha"],
            "pavilion.local_derivative_scene.sha256",
        )
        pavilion_derivative_checked = True

    limitations = [
        (
            "v1 product verification-manifest bytes and verifier PNG digests are "
            "not hashed by the individual benchmark receipts; exact product "
            "metrics are local corroboration, while accepted-draft disposition "
            "is receipt-bound"
        ),
        (
            "Pavilion archive/original-scene hashes, compatibility operations, "
            "and equivalence-probe metrics are declared by the matrix but are "
            "not bound by its benchmark receipt"
        ),
        "all evidence remains local_unattested, preview-only, and non-production",
    ]
    return {
        "schema_version": 1,
        "kind": RESULT_KIND,
        "ok": True,
        "matrix": {
            "path": str(matrix_path.resolve()),
            "sha256": hashlib.sha256(matrix_bytes).hexdigest(),
            "schema_version": matrix["schema_version"],
            "kind": matrix["kind"],
        },
        "receipts": {
            "count": len(facts_rows),
            "hashes_verified": True,
            "shared_execution_identity_verified": True,
            "scene_keys": sorted(seen),
        },
        "local_corroboration": {
            "product_verification_manifests_checked": checked,
            "product_verification_manifests_available": checked == len(facts_rows),
            "product_exact_metrics_receipt_bound": False,
            "pavilion_derivative_scene_checked": pavilion_derivative_checked,
            "pavilion_derivative_scene_receipt_bound": True,
            "pavilion_provenance_block_validated": True,
            "pavilion_provenance_exact_fields_receipt_bound": False,
            "pavilion_source_and_probe_receipt_bound": False,
        },
        "limitations": limitations,
    }


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise MatrixValidationError(f"arguments: {message}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", required=True, type=Path)
    parser.add_argument(
        "--receipts-root",
        type=Path,
        help="receipt path root (default: the matrix's directory)",
    )
    return parser.parse_args(argv)


def _emit(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        result = verify_transfer_matrix(args.matrix, receipts_root=args.receipts_root)
    except (MatrixValidationError, OSError) as exc:
        _emit(
            {
                "schema_version": 1,
                "kind": RESULT_KIND,
                "ok": False,
                "error": {
                    "code": "validation_error",
                    "message": str(exc),
                },
            }
        )
        return 1
    _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
