#!/usr/bin/env python3
"""Build and verify the wire-only v2 full-project render preflight contract.

The contract is deliberately non-executable, non-advertised, non-billable, and
local-unattested.  It verifies a ``cx_render_project_bundle_v1`` snapshot and a
content-addressed project-object file, then binds the object version, scene and
bundle identities, frames, feature cell, output semantics, policies, runtime,
verifier, and resource ceilings into one closed self-hashed document.

This is not production admission.  In particular it does not extract the
project object, run Blender, authorize a final artifact, or release money.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import unicodedata
from typing import Any

import cx_render_project_bundle_v1 as bundle_v1


SCHEMA_VERSION = 2
REQUEST_KIND = "cx_render_project_contract_request"
CONTRACT_KIND = "cx_render_project_wire_preflight"
CONTRACT_TRUST = "local_preflight_unattested"
MAX_REQUEST_BYTES = 1 << 20
MAX_CONTRACT_BYTES = 2 << 20
MAX_TEXT_BYTES = 256
MAX_OBJECT_VERSION_BYTES = 512
MAX_FRAME_ABS = 1_000_000
MAX_FRAMES = 10_000
MAX_DIMENSION = 16_384
MAX_PIXELS_PER_FRAME = MAX_DIMENSION * MAX_DIMENSION
MAX_TOTAL_PIXEL_FRAMES = 1_000_000_000_000
MAX_REFERENCE_SAMPLES = 65_536
MAX_LOW_SAMPLES = 4_096
MAX_OBJECT_BYTES = 2 << 30
MAX_MEMORY_BYTES = 1 << 40
MAX_DISK_BYTES = 16 << 40
MAX_OUTPUT_BYTES = 4 << 40
MAX_PROCESSES = 4_096
MAX_DURATION_SECS = 604_800
MAX_OUTPUT_OBJECTS = 100_000

_REQUEST_KEYS = {
    "schema_version",
    "kind",
    "project_object",
    "frame_range",
    "render",
    "output",
    "policies",
    "runtime",
    "verifier",
    "resources",
}
_PROJECT_OBJECT_KEYS = {"object_key", "object_version", "sha256", "bytes"}
_FRAME_KEYS = {"start", "end", "step", "count"}
_RENDER_KEYS = {
    "feature_cell",
    "engine",
    "device",
    "width",
    "height",
    "camera",
    "view_layer",
    "reference_samples",
    "draft_samples",
    "verify_samples",
    "repair_samples",
    "seed_policy_sha256",
    "use_motion_blur",
    "use_denoising",
}
_OUTPUT_KEYS = {
    "file_format",
    "color_mode",
    "color_depth",
    "codec",
    "passes",
    "views",
    "use_compositor",
    "use_sequencer",
    "use_freestyle",
    "use_multiview",
    "transparent",
    "color_management_sha256",
    "output_policy_sha256",
}
_POLICY_KEYS = {
    "render_policy_sha256",
    "speculation_policy_sha256",
    "fallback_policy_sha256",
    "cache_policy_sha256",
    "fallback_required",
    "fail_closed",
    "requested_quality_tier",
}
_RUNTIME_KEYS = {
    "blender_sha256",
    "runtime_image_sha256",
    "agent_sha256",
    "executor_sha256",
    "dependency_scanner_sha256",
    "sandbox_policy_sha256",
}
_VERIFIER_KEYS = {
    "verifier_sha256",
    "verifier_policy_sha256",
    "selection_policy_sha256",
    "independent_reference_required",
    "server_selected",
}
_RESOURCE_KEYS = {
    "max_duration_secs",
    "max_memory_bytes",
    "max_disk_bytes",
    "max_input_object_bytes",
    "max_project_bytes",
    "max_manifest_bytes",
    "max_output_bytes",
    "max_project_files",
    "max_project_directories",
    "max_frames",
    "max_pixels_per_frame",
    "max_total_pixel_frames",
    "max_processes",
    "max_output_objects",
}
_PROJECT_KEYS = {
    "object_key",
    "object_version",
    "object_sha256",
    "object_bytes",
    "manifest_schema_version",
    "manifest_kind",
    "manifest_sha256",
    "bundle_sha256",
    "scene_path",
    "scene_sha256",
    "file_count",
    "directory_count",
    "total_bytes",
}
_CONTRACT_KEYS = {
    "schema_version",
    "kind",
    "contract_trust",
    "authorization",
    "preflight",
    "project",
    "frame_range",
    "render",
    "output",
    "policies",
    "runtime",
    "verifier",
    "resources",
    "request_sha256",
    "contract_sha256",
}

AUTHORIZATION = {
    "admission": "wire_only",
    "advertised": False,
    "execution_enabled": False,
    "production_ready": False,
    "billing_eligible": False,
    "delivery_eligible": False,
}
PREFLIGHT = {
    "bundle_manifest_verified": True,
    "project_object_bytes_verified": True,
    "object_extraction_verified": False,
    "request_feature_cell_schema_validated": True,
    "final_semantics_execution_verified": False,
}


def _cell(device: str, file_format: str, color_depth: str, codec: str) -> dict[str, Any]:
    return {
        "render": {
            "engine": "CYCLES",
            "device": device,
            "use_motion_blur": False,
            "use_denoising": False,
        },
        "output": {
            "file_format": file_format,
            "color_mode": "RGBA",
            "color_depth": color_depth,
            "codec": codec,
            "passes": ["Combined"],
            "views": ["MAIN"],
            "use_compositor": False,
            "use_sequencer": False,
            "use_freestyle": False,
            "use_multiview": False,
            "transparent": False,
        },
    }


# These are schema/preflight cells, not advertised runtime capabilities.  A
# request outside them fails rather than silently changing final semantics.
FEATURE_CELLS = {
    "cycles_cpu_png_rgba8_combined_no_post_v1": _cell(
        "CPU", "PNG", "8", "DEFLATE_0"
    ),
    "cycles_metal_png_rgba8_combined_no_post_v1": _cell(
        "METAL", "PNG", "8", "DEFLATE_0"
    ),
    "cycles_cpu_openexr_rgba16_combined_no_post_v1": _cell(
        "CPU", "OPEN_EXR", "16", "ZIP"
    ),
    "cycles_metal_openexr_rgba16_combined_no_post_v1": _cell(
        "METAL", "OPEN_EXR", "16", "ZIP"
    ),
}


class ProjectContractError(ValueError):
    """A closed v2 contract, identity, or resource violation."""


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProjectContractError("contract value is not canonical finite JSON") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProjectContractError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ProjectContractError(f"invalid JSON constant {value}")


def _decode(raw: bytes, *, maximum: int, label: str) -> dict[str, Any]:
    if not raw or len(raw) > maximum:
        raise ProjectContractError(f"{label} must contain 1..{maximum} bytes")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except ProjectContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ProjectContractError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ProjectContractError(f"{label} must be a JSON object")
    return value


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ProjectContractError(f"{label} has an unknown or missing field")
    return value


def _sha256(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ProjectContractError(f"{label} must be lowercase SHA-256")
    return value


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProjectContractError(f"{label} must be an integer in [{minimum},{maximum}]")
    return value


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ProjectContractError(f"{label} must be a boolean")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProjectContractError(f"{label} must be a nonempty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProjectContractError(f"{label} must be valid UTF-8") from exc
    if (
        len(encoded) > MAX_TEXT_BYTES
        or unicodedata.normalize("NFC", value) != value
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise ProjectContractError(f"{label} is not a bounded portable string")
    return value


def _validate_project_object(value: Any) -> dict[str, Any]:
    row = _exact(value, _PROJECT_OBJECT_KEYS, "project_object")
    try:
        bundle_v1.strict_relative_path(row["object_key"])
    except bundle_v1.ProjectBundleError as exc:
        raise ProjectContractError(f"project_object.object_key: {exc}") from exc
    version = row["object_version"]
    if (
        not isinstance(version, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~+=:@-]*", version) is None
        or len(version) > MAX_OBJECT_VERSION_BYTES
    ):
        raise ProjectContractError("project_object.object_version is not a bounded opaque version")
    _sha256(row["sha256"], "project_object.sha256")
    _integer(row["bytes"], "project_object.bytes", 1, MAX_OBJECT_BYTES)
    return row


def _validate_frame_range(value: Any) -> dict[str, Any]:
    row = _exact(value, _FRAME_KEYS, "frame_range")
    start = _integer(row["start"], "frame_range.start", -MAX_FRAME_ABS, MAX_FRAME_ABS)
    end = _integer(row["end"], "frame_range.end", -MAX_FRAME_ABS, MAX_FRAME_ABS)
    step = _integer(row["step"], "frame_range.step", 1, 2 * MAX_FRAME_ABS)
    count = _integer(row["count"], "frame_range.count", 1, MAX_FRAMES)
    if end < start or (end - start) % step != 0 or (end - start) // step + 1 != count:
        raise ProjectContractError("frame_range count/end/step arithmetic is inconsistent")
    return row


def _validate_render(value: Any) -> dict[str, Any]:
    row = _exact(value, _RENDER_KEYS, "render")
    feature_cell = row["feature_cell"]
    if not isinstance(feature_cell, str) or feature_cell not in FEATURE_CELLS:
        raise ProjectContractError("render.feature_cell is unsupported and remains unadvertised")
    _text(row["camera"], "render.camera")
    _text(row["view_layer"], "render.view_layer")
    width = _integer(row["width"], "render.width", 16, MAX_DIMENSION)
    height = _integer(row["height"], "render.height", 16, MAX_DIMENSION)
    if width * height > MAX_PIXELS_PER_FRAME:
        raise ProjectContractError("render pixel count exceeds the fixed per-frame bound")
    reference = _integer(
        row["reference_samples"], "render.reference_samples", 2, MAX_REFERENCE_SAMPLES
    )
    draft = _integer(row["draft_samples"], "render.draft_samples", 1, MAX_LOW_SAMPLES)
    verify = _integer(row["verify_samples"], "render.verify_samples", 1, MAX_LOW_SAMPLES)
    repair = _integer(row["repair_samples"], "render.repair_samples", 2, reference)
    if draft >= reference or verify >= reference or repair <= max(draft, verify):
        raise ProjectContractError("render sample ladder is not strictly bounded below reference")
    _sha256(row["seed_policy_sha256"], "render.seed_policy_sha256")
    _boolean(row["use_motion_blur"], "render.use_motion_blur")
    _boolean(row["use_denoising"], "render.use_denoising")
    return row


def _validate_output(value: Any) -> dict[str, Any]:
    row = _exact(value, _OUTPUT_KEYS, "output")
    for field in ("file_format", "color_mode", "color_depth", "codec"):
        _text(row[field], f"output.{field}")
    for field in (
        "use_compositor",
        "use_sequencer",
        "use_freestyle",
        "use_multiview",
        "transparent",
    ):
        _boolean(row[field], f"output.{field}")
    for field in ("passes", "views"):
        values = row[field]
        if not isinstance(values, list) or not values or len(values) > 64:
            raise ProjectContractError(f"output.{field} must be a bounded nonempty array")
        for index, item in enumerate(values):
            _text(item, f"output.{field}[{index}]")
        if len(set(values)) != len(values):
            raise ProjectContractError(f"output.{field} contains a duplicate")
    _sha256(row["color_management_sha256"], "output.color_management_sha256")
    _sha256(row["output_policy_sha256"], "output.output_policy_sha256")
    return row


def _validate_feature_cell(render: dict[str, Any], output: dict[str, Any]) -> None:
    cell = FEATURE_CELLS[render["feature_cell"]]
    for field, expected in cell["render"].items():
        if render[field] != expected:
            raise ProjectContractError(
                f"render.{field} contradicts feature cell {render['feature_cell']!r}"
            )
    for field, expected in cell["output"].items():
        if output[field] != expected:
            raise ProjectContractError(
                f"output.{field} contradicts feature cell {render['feature_cell']!r}"
            )


def _validate_policies(value: Any) -> dict[str, Any]:
    row = _exact(value, _POLICY_KEYS, "policies")
    for field in (
        "render_policy_sha256",
        "speculation_policy_sha256",
        "fallback_policy_sha256",
        "cache_policy_sha256",
    ):
        _sha256(row[field], f"policies.{field}")
    if _boolean(row["fallback_required"], "policies.fallback_required") is not True:
        raise ProjectContractError("full-project wire cells require fallback_required=true")
    if _boolean(row["fail_closed"], "policies.fail_closed") is not True:
        raise ProjectContractError("full-project wire cells require fail_closed=true")
    if row["requested_quality_tier"] != "delivery":
        raise ProjectContractError("requested_quality_tier must be delivery")
    return row


def _validate_runtime(value: Any) -> dict[str, Any]:
    row = _exact(value, _RUNTIME_KEYS, "runtime")
    for field in _RUNTIME_KEYS:
        _sha256(row[field], f"runtime.{field}")
    return row


def _validate_verifier(value: Any) -> dict[str, Any]:
    row = _exact(value, _VERIFIER_KEYS, "verifier")
    for field in (
        "verifier_sha256",
        "verifier_policy_sha256",
        "selection_policy_sha256",
    ):
        _sha256(row[field], f"verifier.{field}")
    if (
        _boolean(
            row["independent_reference_required"],
            "verifier.independent_reference_required",
        )
        is not True
        or _boolean(row["server_selected"], "verifier.server_selected") is not True
    ):
        raise ProjectContractError(
            "full-project wire cells require server-selected independent verification"
        )
    return row


def _validate_resources(value: Any) -> dict[str, Any]:
    row = _exact(value, _RESOURCE_KEYS, "resources")
    ranges = {
        "max_duration_secs": (1, MAX_DURATION_SECS),
        "max_memory_bytes": (1 << 30, MAX_MEMORY_BYTES),
        "max_disk_bytes": (1 << 20, MAX_DISK_BYTES),
        "max_input_object_bytes": (1, MAX_OBJECT_BYTES),
        "max_project_bytes": (1, bundle_v1.MAX_TOTAL_BYTES),
        "max_manifest_bytes": (1, bundle_v1.MAX_MANIFEST_BYTES),
        "max_output_bytes": (1, MAX_OUTPUT_BYTES),
        "max_project_files": (1, bundle_v1.MAX_FILES),
        "max_project_directories": (0, bundle_v1.MAX_DIRECTORIES - 1),
        "max_frames": (1, MAX_FRAMES),
        "max_pixels_per_frame": (256, MAX_PIXELS_PER_FRAME),
        "max_total_pixel_frames": (256, MAX_TOTAL_PIXEL_FRAMES),
        "max_processes": (1, MAX_PROCESSES),
        "max_output_objects": (1, MAX_OUTPUT_OBJECTS),
    }
    for field, (minimum, maximum) in ranges.items():
        _integer(row[field], f"resources.{field}", minimum, maximum)
    return row


def parse_request(raw: bytes) -> dict[str, Any]:
    request = _decode(raw, maximum=MAX_REQUEST_BYTES, label="contract request")
    _exact(request, _REQUEST_KEYS, "contract request")
    if (
        type(request["schema_version"]) is not int
        or request["schema_version"] != SCHEMA_VERSION
        or request["kind"] != REQUEST_KIND
    ):
        raise ProjectContractError("contract request schema_version/kind mismatch")
    _validate_project_object(request["project_object"])
    _validate_frame_range(request["frame_range"])
    render = _validate_render(request["render"])
    output = _validate_output(request["output"])
    _validate_feature_cell(render, output)
    _validate_policies(request["policies"])
    _validate_runtime(request["runtime"])
    _validate_verifier(request["verifier"])
    _validate_resources(request["resources"])
    return request


def _file_identity(info: os.stat_result) -> tuple[int, ...]:
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


def _hash_project_object(path: Path, maximum: int) -> dict[str, Any]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ProjectContractError(f"cannot open project object {path}: {exc}") from exc
    digest = hashlib.sha256()
    total = 0
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ProjectContractError("project object must be one non-hard-linked regular file")
        if before.st_size > maximum:
            raise ProjectContractError("project object exceeds its declared resource bound")
        while True:
            chunk = os.read(descriptor, 1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise ProjectContractError("project object exceeds its declared resource bound")
            digest.update(chunk)
        after = os.fstat(descriptor)
        identity = _file_identity(after)
        if _file_identity(before) != identity or total != after.st_size:
            raise ProjectContractError("project object changed while hashed")
    finally:
        os.close(descriptor)
    try:
        current = path.lstat()
    except OSError as exc:
        raise ProjectContractError("project object disappeared after hashing") from exc
    if (
        not stat.S_ISREG(current.st_mode)
        or path.is_symlink()
        or _file_identity(current) != identity
    ):
        raise ProjectContractError("project object path changed after hashing")
    return {"sha256": digest.hexdigest(), "bytes": total}


def _outside_project(path: Path | str, root: Path | str, label: str) -> Path:
    project_root = Path(os.path.abspath(os.fspath(root)))
    candidate = Path(os.path.abspath(os.fspath(path)))
    try:
        parent = candidate.parent.resolve(strict=True)
        resolved_root = project_root.resolve(strict=True)
    except OSError as exc:
        raise ProjectContractError(f"{label} parent/root is unavailable: {exc}") from exc
    resolved = parent / candidate.name
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        return resolved
    raise ProjectContractError(f"{label} must live outside the project root")


def _load_verified_inputs(
    root: Path | str,
    manifest_raw: bytes,
    object_path: Path | str,
    request: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        manifest = bundle_v1.verify_manifest(root, manifest_raw)
    except bundle_v1.ProjectBundleError as exc:
        raise ProjectContractError(f"bundle manifest verification failed: {exc}") from exc
    object_file = _outside_project(object_path, root, "project object")
    object_identity = _hash_project_object(
        object_file, request["resources"]["max_input_object_bytes"]
    )
    declared = request["project_object"]
    if (
        object_identity["sha256"] != declared["sha256"]
        or object_identity["bytes"] != declared["bytes"]
    ):
        raise ProjectContractError("project object bytes contradict the request identity")
    _cross_validate(request, manifest, len(manifest_raw), object_identity)
    return manifest, object_identity


def _cross_validate(
    request: dict[str, Any],
    manifest: dict[str, Any],
    manifest_bytes: int,
    object_identity: dict[str, Any],
) -> None:
    resources = request["resources"]
    frames = request["frame_range"]["count"]
    pixels = request["render"]["width"] * request["render"]["height"]
    total_pixel_frames = pixels * frames
    if object_identity["bytes"] > resources["max_input_object_bytes"]:
        raise ProjectContractError("project object exceeds max_input_object_bytes")
    if manifest["total_bytes"] > resources["max_project_bytes"]:
        raise ProjectContractError("bundle exceeds max_project_bytes")
    if manifest_bytes > resources["max_manifest_bytes"]:
        raise ProjectContractError("manifest exceeds max_manifest_bytes")
    if manifest["file_count"] > resources["max_project_files"]:
        raise ProjectContractError("bundle exceeds max_project_files")
    if manifest["directory_count"] > resources["max_project_directories"]:
        raise ProjectContractError("bundle exceeds max_project_directories")
    if frames > resources["max_frames"]:
        raise ProjectContractError("frame range exceeds max_frames")
    if pixels > resources["max_pixels_per_frame"]:
        raise ProjectContractError("render exceeds max_pixels_per_frame")
    if total_pixel_frames > resources["max_total_pixel_frames"]:
        raise ProjectContractError("render exceeds max_total_pixel_frames")
    depth_bytes = int(request["output"]["color_depth"]) // 8
    minimum_output = pixels * 4 * depth_bytes * frames + (1 << 20) * frames
    if resources["max_output_bytes"] < minimum_output:
        raise ProjectContractError("max_output_bytes cannot contain the requested raw output bound")
    output_objects = frames * len(request["output"]["passes"]) * len(
        request["output"]["views"]
    )
    if resources["max_output_objects"] < output_objects:
        raise ProjectContractError("max_output_objects cannot contain the requested outputs")
    required_disk = (
        object_identity["bytes"] + manifest["total_bytes"] + resources["max_output_bytes"]
    )
    if resources["max_disk_bytes"] < required_disk:
        raise ProjectContractError("max_disk_bytes cannot contain input, extraction, and output")


def _request_sha256(request: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(request)).hexdigest()


def _contract_sha256(contract: dict[str, Any]) -> str:
    unsigned = dict(contract)
    unsigned.pop("contract_sha256", None)
    return hashlib.sha256(canonical_json(unsigned)).hexdigest()


def build_contract(
    root: Path | str,
    manifest_raw: bytes,
    object_path: Path | str,
    request_raw: bytes,
) -> dict[str, Any]:
    request = parse_request(request_raw)
    manifest, object_identity = _load_verified_inputs(
        root, manifest_raw, object_path, request
    )
    declared_object = request["project_object"]
    contract: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": CONTRACT_KIND,
        "contract_trust": CONTRACT_TRUST,
        "authorization": dict(AUTHORIZATION),
        "preflight": dict(PREFLIGHT),
        "project": {
            "object_key": declared_object["object_key"],
            "object_version": declared_object["object_version"],
            "object_sha256": object_identity["sha256"],
            "object_bytes": object_identity["bytes"],
            "manifest_schema_version": manifest["schema_version"],
            "manifest_kind": manifest["kind"],
            "manifest_sha256": manifest["manifest_sha256"],
            "bundle_sha256": manifest["bundle_sha256"],
            "scene_path": manifest["scene_path"],
            "scene_sha256": manifest["scene_sha256"],
            "file_count": manifest["file_count"],
            "directory_count": manifest["directory_count"],
            "total_bytes": manifest["total_bytes"],
        },
        "frame_range": request["frame_range"],
        "render": request["render"],
        "output": request["output"],
        "policies": request["policies"],
        "runtime": request["runtime"],
        "verifier": request["verifier"],
        "resources": request["resources"],
        "request_sha256": _request_sha256(request),
    }
    contract["contract_sha256"] = _contract_sha256(contract)
    if len(canonical_json(contract)) > MAX_CONTRACT_BYTES:
        raise ProjectContractError(f"contract exceeds {MAX_CONTRACT_BYTES} bytes")
    return contract


def parse_contract(raw: bytes) -> dict[str, Any]:
    contract = _decode(raw, maximum=MAX_CONTRACT_BYTES, label="project contract")
    _exact(contract, _CONTRACT_KEYS, "project contract")
    if (
        type(contract["schema_version"]) is not int
        or contract["schema_version"] != SCHEMA_VERSION
        or contract["kind"] != CONTRACT_KIND
        or contract["contract_trust"] != CONTRACT_TRUST
    ):
        raise ProjectContractError("project contract schema/kind/trust mismatch")
    if contract["authorization"] != AUTHORIZATION:
        raise ProjectContractError("project contract tried to escape wire-only authorization")
    if contract["preflight"] != PREFLIGHT:
        raise ProjectContractError("project contract preflight claims unsupported completion")
    project = _exact(contract["project"], _PROJECT_KEYS, "project")
    for field in (
        "object_sha256",
        "manifest_sha256",
        "bundle_sha256",
        "scene_sha256",
    ):
        _sha256(project[field], f"project.{field}")
    try:
        bundle_v1.strict_relative_path(project["object_key"])
        bundle_v1.strict_relative_path(project["scene_path"], require_blend=True)
    except bundle_v1.ProjectBundleError as exc:
        raise ProjectContractError(f"project path is invalid: {exc}") from exc
    if (
        not isinstance(project["object_version"], str)
        or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._~+=:@-]*", project["object_version"]
        )
        is None
        or len(project["object_version"]) > MAX_OBJECT_VERSION_BYTES
    ):
        raise ProjectContractError("project.object_version is malformed")
    _integer(project["object_bytes"], "project.object_bytes", 1, MAX_OBJECT_BYTES)
    _integer(
        project["manifest_schema_version"],
        "project.manifest_schema_version",
        bundle_v1.SCHEMA_VERSION,
        bundle_v1.SCHEMA_VERSION,
    )
    if project["manifest_kind"] != bundle_v1.MANIFEST_KIND:
        raise ProjectContractError("project.manifest_kind mismatch")
    _integer(project["file_count"], "project.file_count", 1, bundle_v1.MAX_FILES)
    _integer(
        project["directory_count"],
        "project.directory_count",
        0,
        bundle_v1.MAX_DIRECTORIES - 1,
    )
    _integer(
        project["total_bytes"], "project.total_bytes", 1, bundle_v1.MAX_TOTAL_BYTES
    )
    _sha256(contract["request_sha256"], "request_sha256")
    _sha256(contract["contract_sha256"], "contract_sha256")
    if _contract_sha256(contract) != contract["contract_sha256"]:
        raise ProjectContractError("project contract self SHA is invalid")

    request = _request_from_contract(contract)
    parse_request(canonical_json(request))
    if _request_sha256(request) != contract["request_sha256"]:
        raise ProjectContractError("project contract request SHA is invalid")
    return contract


def _request_from_contract(contract: dict[str, Any]) -> dict[str, Any]:
    project = contract["project"]
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": REQUEST_KIND,
        "project_object": {
            "object_key": project["object_key"],
            "object_version": project["object_version"],
            "sha256": project["object_sha256"],
            "bytes": project["object_bytes"],
        },
        "frame_range": contract["frame_range"],
        "render": contract["render"],
        "output": contract["output"],
        "policies": contract["policies"],
        "runtime": contract["runtime"],
        "verifier": contract["verifier"],
        "resources": contract["resources"],
    }


def verify_contract(
    root: Path | str,
    manifest_raw: bytes,
    object_path: Path | str,
    request_raw: bytes,
    contract_raw: bytes,
) -> dict[str, Any]:
    request = parse_request(request_raw)
    contract = parse_contract(contract_raw)
    if _request_from_contract(contract) != request:
        raise ProjectContractError("project contract does not match the original request")
    if contract["request_sha256"] != _request_sha256(request):
        raise ProjectContractError("project contract does not bind the original request")
    manifest, object_identity = _load_verified_inputs(
        root, manifest_raw, object_path, request
    )
    project = contract["project"]
    expected_project = {
        "object_key": request["project_object"]["object_key"],
        "object_version": request["project_object"]["object_version"],
        "object_sha256": object_identity["sha256"],
        "object_bytes": object_identity["bytes"],
        "manifest_schema_version": manifest["schema_version"],
        "manifest_kind": manifest["kind"],
        "manifest_sha256": manifest["manifest_sha256"],
        "bundle_sha256": manifest["bundle_sha256"],
        "scene_path": manifest["scene_path"],
        "scene_sha256": manifest["scene_sha256"],
        "file_count": manifest["file_count"],
        "directory_count": manifest["directory_count"],
        "total_bytes": manifest["total_bytes"],
    }
    if project != expected_project:
        raise ProjectContractError("project contract identity contradicts verified inputs")
    return contract


def _read_outside(path: Path, root: Path, maximum: int, label: str) -> bytes:
    outside = _outside_project(path, root, label)
    try:
        return bundle_v1._read_bounded_file(outside, maximum)
    except bundle_v1.ProjectBundleError as exc:
        raise ProjectContractError(f"cannot read {label}: {exc}") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="create a new wire-only contract")
    verify = commands.add_parser("verify", help="replay all v2 preflight bindings")
    for command in (create, verify):
        command.add_argument("--root", required=True, type=Path)
        command.add_argument("--manifest", required=True, type=Path)
        command.add_argument("--object", required=True, type=Path)
        command.add_argument("--request", required=True, type=Path)
    create.add_argument("--output", required=True, type=Path)
    verify.add_argument("--contract", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = bundle_v1._project_root(args.root)
        manifest_raw = _read_outside(
            args.manifest, root, bundle_v1.MAX_MANIFEST_BYTES, "bundle manifest"
        )
        request_raw = _read_outside(args.request, root, MAX_REQUEST_BYTES, "contract request")
        object_path = _outside_project(args.object, root, "project object")
        if args.command == "create":
            contract = build_contract(root, manifest_raw, object_path, request_raw)
            encoded = canonical_json(contract) + b"\n"
            output = _outside_project(args.output, root, "contract output")
            try:
                bundle_v1._publish_new(output, encoded, root)
            except bundle_v1.ProjectBundleError as exc:
                raise ProjectContractError(f"contract publication failed: {exc}") from exc
            return 0
        contract_raw = _read_outside(
            args.contract, root, MAX_CONTRACT_BYTES, "project contract"
        )
        contract = verify_contract(
            root, manifest_raw, object_path, request_raw, contract_raw
        )
        proof = {
            "ok": True,
            "kind": CONTRACT_KIND,
            "admission": "wire_only",
            "contract_sha256": contract["contract_sha256"],
            "request_sha256": contract["request_sha256"],
            "manifest_sha256": contract["project"]["manifest_sha256"],
            "bundle_sha256": contract["project"]["bundle_sha256"],
            "object_version": contract["project"]["object_version"],
        }
        sys.stdout.buffer.write(canonical_json(proof) + b"\n")
        sys.stdout.buffer.flush()
        return 0
    except (ProjectContractError, bundle_v1.ProjectBundleError) as exc:
        print(f"project contract rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
