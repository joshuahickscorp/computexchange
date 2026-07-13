#!/usr/bin/env python3
"""Empirical Blender screen for narrowing resident Cycles invalidation.

Run this file inside Blender, for example::

    Blender -b --python scripts/spec-lab/screen_blender_resident_invalidation.py -- \
      --scene /absolute/scene.blend --output-dir /absolute/new-output-dir \
      --width 512 --height 288 --frame 1 --samples 8 --device METAL

The screen deliberately makes no performance or correctness claim about the
production preview backend. It renders an A -> B -> A sequence across a 2x3
factorial: redundant same-frame ``frame_set`` versus no ``frame_set``, crossed
with broad, TIME-only, and absent ID tags. The broad ``frame_set`` +
``scene.update_tag()`` path is the oracle. A narrower mode is valid only when
its three decoded outputs are tightly semantically equivalent to the oracle,
its repeated A output is byte-for-byte pixel stable, and the disjoint B sample
range is observably different from A. Exact cross-mode hashes remain recorded
as diagnostics because Metal can drift by a handful of pixels across rebuilds.

The decision and report validators are pure Python so their unit tests do not
need Blender.  ``bpy`` is imported only at the runtime boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import struct
import sys
import time
from typing import Any
import zlib


KIND = "cx_blender_resident_invalidation_screen"
SCHEMA_VERSION = 1
# A 2x3 factorial isolates the redundant same-frame ``frame_set`` from ID-tag
# invalidation. ``full`` remains the broad production-behavior oracle.
MODE_SPECS = {
    "full": {"frame_set_per_render": True, "tag_mode": "full"},
    "none": {"frame_set_per_render": True, "tag_mode": "none"},
    "time": {"frame_set_per_render": True, "tag_mode": "time"},
    "full_no_frame_set": {"frame_set_per_render": False, "tag_mode": "full"},
    "none_no_frame_set": {"frame_set_per_render": False, "tag_mode": "none"},
    "time_no_frame_set": {"frame_set_per_render": False, "tag_mode": "time"},
}
MODE_ORDER = tuple(MODE_SPECS)
FOLLOWUP_PREFERENCE = (
    "none_no_frame_set",
    "time_no_frame_set",
    "full_no_frame_set",
    "none",
    "time",
)
RENDER_ORDER = ("a1", "b", "a2")
INTEGRATOR_KEYS = (
    "max_bounces",
    "diffuse_bounces",
    "glossy_bounces",
    "transmission_bounces",
)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_DIMENSION = 4096
MAX_PIXELS = 16_777_216
MAX_SAMPLES = 4096
SEMANTIC_AGREEMENT_MIN = 0.99999
SEMANTIC_CHANGED_PIXEL_FRACTION_MAX = 0.0001
INDEPENDENCE_AGREEMENT_MAX = 0.999
INDEPENDENCE_CHANGED_PIXEL_FRACTION_MIN = 0.001


class ScreenError(ValueError):
    """A malformed input, render artifact, or screen report."""


def _round9(value: float) -> float:
    if not math.isfinite(value):
        raise ScreenError("metric is non-finite")
    return float(f"{value:.9f}")


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), total


def _paeth(left: int, up: int, upper_left: int) -> int:
    estimate = left + up - upper_left
    left_distance = abs(estimate - left)
    up_distance = abs(estimate - up)
    upper_left_distance = abs(estimate - upper_left)
    if left_distance <= up_distance and left_distance <= upper_left_distance:
        return left
    if up_distance <= upper_left_distance:
        return up
    return upper_left


def _decode_png_rgb(
    path: Path, *, expected_width: int, expected_height: int
) -> bytes:
    """Decode the exact 8-bit, non-interlaced PNG subset Blender emits."""
    data = path.read_bytes()
    if not data.startswith(PNG_SIGNATURE):
        raise ScreenError(f"{path}: output is not a PNG")
    cursor = len(PNG_SIGNATURE)
    header: tuple[int, int, int, int, int, int, int] | None = None
    compressed = bytearray()
    saw_end = False
    while cursor < len(data):
        if cursor + 12 > len(data):
            raise ScreenError(f"{path}: truncated PNG chunk")
        length = struct.unpack(">I", data[cursor : cursor + 4])[0]
        kind = data[cursor + 4 : cursor + 8]
        payload_start = cursor + 8
        payload_end = payload_start + length
        crc_end = payload_end + 4
        if crc_end > len(data):
            raise ScreenError(f"{path}: truncated PNG payload")
        payload = data[payload_start:payload_end]
        expected_crc = struct.unpack(">I", data[payload_end:crc_end])[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != expected_crc:
            raise ScreenError(f"{path}: PNG CRC mismatch")
        if kind == b"IHDR":
            if header is not None or length != 13:
                raise ScreenError(f"{path}: invalid PNG header")
            header = struct.unpack(">IIBBBBB", payload)
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            if length != 0:
                raise ScreenError(f"{path}: invalid PNG end chunk")
            saw_end = True
            if crc_end != len(data):
                raise ScreenError(f"{path}: trailing bytes after PNG end")
            break
        cursor = crc_end
    if header is None or not saw_end or not compressed:
        raise ScreenError(f"{path}: incomplete PNG")
    width, height, depth, color_type, compression, filtering, interlace = header
    if (width, height) != (expected_width, expected_height):
        raise ScreenError(f"{path}: PNG dimensions changed")
    if (
        depth != 8
        or color_type not in {2, 6}
        or compression != 0
        or filtering != 0
        or interlace != 0
    ):
        raise ScreenError(f"{path}: unsupported Blender PNG encoding")
    channels = 3 if color_type == 2 else 4
    stride = width * channels
    expected_bytes = height * (stride + 1)
    try:
        raw = zlib.decompress(bytes(compressed))
    except zlib.error as exc:
        raise ScreenError(f"{path}: PNG deflate failed: {exc}") from exc
    if len(raw) != expected_bytes:
        raise ScreenError(f"{path}: decoded PNG byte count changed")

    previous = bytearray(stride)
    rgb = bytearray(width * height * 3)
    source_cursor = 0
    rgb_cursor = 0
    for _row in range(height):
        filter_type = raw[source_cursor]
        source_cursor += 1
        encoded = raw[source_cursor : source_cursor + stride]
        source_cursor += stride
        decoded = bytearray(stride)
        for index, encoded_value in enumerate(encoded):
            left = decoded[index - channels] if index >= channels else 0
            up = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 0:
                value = encoded_value
            elif filter_type == 1:
                value = encoded_value + left
            elif filter_type == 2:
                value = encoded_value + up
            elif filter_type == 3:
                value = encoded_value + ((left + up) >> 1)
            elif filter_type == 4:
                value = encoded_value + _paeth(left, up, upper_left)
            else:
                raise ScreenError(f"{path}: unsupported PNG row filter")
            decoded[index] = value & 0xFF
        for column in range(width):
            start = column * channels
            rgb[rgb_cursor : rgb_cursor + 3] = decoded[start : start + 3]
            rgb_cursor += 3
        previous = decoded
    return bytes(rgb)


def _rgb_difference_stats(left: bytes, right: bytes) -> dict[str, Any]:
    if not left or len(left) != len(right) or len(left) % 3:
        raise ScreenError("decoded RGB buffers are incompatible")
    difference = 0
    changed_pixels = 0
    maximum = 0
    for index in range(0, len(left), 3):
        red = abs(left[index] - right[index])
        green = abs(left[index + 1] - right[index + 1])
        blue = abs(left[index + 2] - right[index + 2])
        pixel_maximum = max(red, green, blue)
        difference += red + green + blue
        maximum = max(maximum, pixel_maximum)
        if pixel_maximum:
            changed_pixels += 1
    agreement = _round9(1.0 - difference / (len(left) * 255.0))
    changed_fraction = _round9(changed_pixels / (len(left) // 3))
    return {
        "agreement": agreement,
        "changed_pixel_fraction": changed_fraction,
        "changed_pixels": changed_pixels,
        "max_abs_channel_difference": maximum,
        "semantic_equivalent": (
            agreement >= SEMANTIC_AGREEMENT_MIN
            and changed_fraction <= SEMANTIC_CHANGED_PIXEL_FRACTION_MAX
        ),
    }


def _comparison(
    left: dict[str, Any], right: dict[str, Any], stats: dict[str, Any]
) -> dict[str, Any]:
    identical = left["decoded_rgb_sha256"] == right["decoded_rgb_sha256"]
    return {
        **stats,
        "left": left["label"],
        "pixel_identical": identical,
        "right": right["label"],
    }


def _records_by_label(mode: dict[str, Any]) -> dict[str, dict[str, Any]]:
    records = mode.get("renders")
    if not isinstance(records, list):
        return {}
    output: dict[str, dict[str, Any]] = {}
    for record in records:
        if isinstance(record, dict) and isinstance(record.get("label"), str):
            output[record["label"]] = record
    return output


def evaluate_invalidation(modes: dict[str, Any]) -> dict[str, Any]:
    """Return a fail-closed decision from render-record decoded hashes."""
    if set(modes) != set(MODE_ORDER):
        raise ScreenError("mode set is not closed")
    full = modes["full"]
    full_records = _records_by_label(full)
    full_reasons: list[str] = []
    if full.get("supported") is not True or full.get("error") is not None:
        full_reasons.append("full_mode_failed")
    if set(full_records) != set(RENDER_ORDER):
        full_reasons.append("full_render_set_incomplete")
    else:
        full_pairs = full.get("pair_agreements")
        repeat = (
            full_pairs.get("a1_vs_a2")
            if isinstance(full_pairs, dict)
            else None
        )
        independent = (
            full_pairs.get("a1_vs_b")
            if isinstance(full_pairs, dict)
            else None
        )
        if not isinstance(repeat, dict) or repeat.get("semantic_equivalent") is not True:
            full_reasons.append("full_same_config_not_repeatable")
        if (
            not isinstance(independent, dict)
            or not isinstance(independent.get("agreement"), (int, float))
            or independent["agreement"] > INDEPENDENCE_AGREEMENT_MAX
            or not isinstance(independent.get("changed_pixel_fraction"), (int, float))
            or independent["changed_pixel_fraction"]
            < INDEPENDENCE_CHANGED_PIXEL_FRACTION_MIN
        ):
            full_reasons.append("full_disjoint_range_did_not_change_pixels")
        if full_records["a1"].get("sample_range") != [0, full_records["a1"].get("samples")]:
            full_reasons.append("full_a_sample_range_invalid")
        samples = full_records["b"].get("samples")
        expected_b_range = [samples, 2 * samples] if isinstance(samples, int) else None
        if expected_b_range is None or full_records["b"].get("sample_range") != expected_b_range:
            full_reasons.append("full_b_sample_range_invalid")
    full_valid = not full_reasons

    decisions: dict[str, dict[str, Any]] = {
        "full": {
            "reasons": full_reasons,
            "valid_oracle": full_valid,
        }
    }
    valid_narrower: list[str] = []
    for name in MODE_ORDER[1:]:
        mode = modes[name]
        records = _records_by_label(mode)
        reasons: list[str] = []
        if not full_valid:
            reasons.append("full_oracle_invalid")
        if mode.get("supported") is not True:
            reasons.append("mode_unsupported")
        if mode.get("error") is not None:
            reasons.append("mode_failed")
        if set(records) != set(RENDER_ORDER):
            reasons.append("render_set_incomplete")
        elif full_valid:
            pairs = mode.get("pair_agreements")
            repeat = pairs.get("a1_vs_a2") if isinstance(pairs, dict) else None
            independent = pairs.get("a1_vs_b") if isinstance(pairs, dict) else None
            if (
                not isinstance(repeat, dict)
                or repeat.get("semantic_equivalent") is not True
            ):
                reasons.append("same_config_not_repeatable")
            oracle = mode.get("oracle_agreements")
            if not isinstance(oracle, dict) or set(oracle) != set(RENDER_ORDER):
                reasons.append("oracle_comparisons_incomplete")
            else:
                for label in RENDER_ORDER:
                    comparison = oracle[label]
                    if not isinstance(comparison, dict) or comparison.get(
                        "semantic_equivalent"
                    ) is not True:
                        reasons.append(f"{label}_differs_from_full_oracle")
            if (
                not isinstance(independent, dict)
                or not isinstance(independent.get("agreement"), (int, float))
                or independent["agreement"] > INDEPENDENCE_AGREEMENT_MAX
                or not isinstance(
                    independent.get("changed_pixel_fraction"), (int, float)
                )
                or independent["changed_pixel_fraction"]
                < INDEPENDENCE_CHANGED_PIXEL_FRACTION_MIN
            ):
                reasons.append("disjoint_range_did_not_change_pixels")
            if records["a1"].get("sample_range") != full_records["a1"].get(
                "sample_range"
            ) or records["b"].get("sample_range") != full_records["b"].get(
                "sample_range"
            ):
                reasons.append("sample_ranges_differ_from_oracle")
        reasons = list(dict.fromkeys(reasons))
        valid = not reasons
        decisions[name] = {
            "reasons": reasons,
            "valid_narrower_mode": valid,
        }
        if valid:
            valid_narrower.append(name)
    recommended = next(
        (name for name in FOLLOWUP_PREFERENCE if name in valid_narrower),
        "full",
    )
    return {
        "full_anchor_valid": full_valid,
        "modes": decisions,
        "recommended_mode_for_followup_only": recommended,
        "valid_narrower_modes": valid_narrower,
    }


def _require_hash(value: Any, location: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise ScreenError(f"{location} is not a SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ScreenError(f"{location} is not hexadecimal") from exc


def _validate_comparison(
    comparison: Any,
    left: dict[str, Any],
    right: dict[str, Any],
    location: str,
) -> None:
    if not isinstance(comparison, dict) or set(comparison) != {
        "agreement", "changed_pixel_fraction", "changed_pixels", "left",
        "max_abs_channel_difference", "pixel_identical", "right",
        "semantic_equivalent"
    }:
        raise ScreenError(f"{location} shape mismatch")
    agreement = comparison["agreement"]
    if (
        not isinstance(agreement, (int, float))
        or isinstance(agreement, bool)
        or not math.isfinite(float(agreement))
        or not 0.0 <= agreement <= 1.0
    ):
        raise ScreenError(f"{location}.agreement is invalid")
    if comparison["left"] != left["label"] or comparison["right"] != right["label"]:
        raise ScreenError(f"{location} label mismatch")
    if (
        not isinstance(comparison["changed_pixels"], int)
        or isinstance(comparison["changed_pixels"], bool)
        or comparison["changed_pixels"] < 0
        or not isinstance(comparison["changed_pixel_fraction"], (int, float))
        or isinstance(comparison["changed_pixel_fraction"], bool)
        or not math.isfinite(float(comparison["changed_pixel_fraction"]))
        or not 0.0 <= comparison["changed_pixel_fraction"] <= 1.0
        or not isinstance(comparison["max_abs_channel_difference"], int)
        or isinstance(comparison["max_abs_channel_difference"], bool)
        or not 0 <= comparison["max_abs_channel_difference"] <= 255
    ):
        raise ScreenError(f"{location} difference statistics are invalid")
    identical = left["decoded_rgb_sha256"] == right["decoded_rgb_sha256"]
    if comparison["pixel_identical"] is not identical:
        raise ScreenError(f"{location}.pixel_identical disagrees with hashes")
    # A one-code-value difference in a very large image can round to 1.0 at
    # nine decimals, so only the forward implication is safe here. Decisive
    # identity always uses the decoded hash.
    if identical and agreement != 1.0:
        raise ScreenError(f"{location}.agreement disagrees with hashes")
    if identical and (
        comparison["changed_pixels"] != 0
        or comparison["changed_pixel_fraction"] != 0.0
        or comparison["max_abs_channel_difference"] != 0
    ):
        raise ScreenError(f"{location} exact hashes have nonzero difference statistics")
    expected_semantic = (
        agreement >= SEMANTIC_AGREEMENT_MIN
        and comparison["changed_pixel_fraction"]
        <= SEMANTIC_CHANGED_PIXEL_FRACTION_MAX
    )
    if comparison["semantic_equivalent"] is not expected_semantic:
        raise ScreenError(f"{location}.semantic_equivalent is inconsistent")


def validate_report(report: dict[str, Any]) -> None:
    """Validate decisive report structure and recompute the decision."""
    required = {
        "blender",
        "configuration",
        "decision",
        "experimental_only",
        "host",
        "kind",
        "limitations",
        "modes",
        "scene",
        "schema_version",
        "warmup",
    }
    if not isinstance(report, dict) or set(report) != required:
        raise ScreenError("report shape is not closed")
    if report["schema_version"] != SCHEMA_VERSION or report["kind"] != KIND:
        raise ScreenError("report identity mismatch")
    if report["experimental_only"] is not True:
        raise ScreenError("screen must remain experimental-only")
    modes = report["modes"]
    if not isinstance(modes, dict) or set(modes) != set(MODE_ORDER):
        raise ScreenError("report mode set mismatch")
    for mode_name in MODE_ORDER:
        mode = modes[mode_name]
        if not isinstance(mode, dict) or set(mode) != {
            "error", "frame_action", "frame_set_per_render", "mode",
            "oracle_agreements", "pair_agreements", "renders", "supported",
            "tag_action"
        }:
            raise ScreenError(f"modes.{mode_name} shape mismatch")
        if mode["mode"] != mode_name or type(mode["supported"]) is not bool:
            raise ScreenError(f"modes.{mode_name} identity mismatch")
        expected_frame_set = MODE_SPECS[mode_name]["frame_set_per_render"]
        if mode["frame_set_per_render"] is not expected_frame_set:
            raise ScreenError(f"modes.{mode_name}.frame_set_per_render mismatch")
        if not isinstance(mode["renders"], list):
            raise ScreenError(f"modes.{mode_name}.renders must be a list")
        labels: list[str] = []
        for record in mode["renders"]:
            if not isinstance(record, dict):
                raise ScreenError(f"modes.{mode_name}.renders entry is not an object")
            keys = {
                "bytes", "config", "decoded_rgb_sha256", "height", "label",
                "path", "sample_offset", "sample_range", "samples", "seed",
                "sha256", "wall_s", "width",
            }
            if set(record) != keys:
                raise ScreenError(f"modes.{mode_name}.render shape mismatch")
            labels.append(record["label"])
            _require_hash(record["sha256"], f"modes.{mode_name}.render.sha256")
            _require_hash(
                record["decoded_rgb_sha256"],
                f"modes.{mode_name}.render.decoded_rgb_sha256",
            )
            if (
                not isinstance(record["wall_s"], (int, float))
                or isinstance(record["wall_s"], bool)
                or not math.isfinite(float(record["wall_s"]))
                or record["wall_s"] <= 0
            ):
                raise ScreenError(f"modes.{mode_name}.render.wall_s is invalid")
        if labels and labels != list(RENDER_ORDER):
            if labels != list(RENDER_ORDER[: len(labels)]):
                raise ScreenError(f"modes.{mode_name}.render order mismatch")
        by_label = {record["label"]: record for record in mode["renders"]}
        expected_pairs = {
            key: (by_label[left], by_label[right])
            for key, left, right in (
                ("a1_vs_a2", "a1", "a2"),
                ("a1_vs_b", "a1", "b"),
                ("b_vs_a2", "b", "a2"),
            )
            if left in by_label and right in by_label
        }
        if set(mode["pair_agreements"]) != set(expected_pairs):
            raise ScreenError(f"modes.{mode_name}.pair_agreements shape mismatch")
        for key, (left, right) in expected_pairs.items():
            _validate_comparison(
                mode["pair_agreements"][key],
                left,
                right,
                f"modes.{mode_name}.pair_agreements.{key}",
            )
    full_by_label = {
        record["label"]: record for record in modes["full"]["renders"]
    }
    for mode_name in MODE_ORDER:
        mode = modes[mode_name]
        by_label = {record["label"]: record for record in mode["renders"]}
        expected_labels = (
            set(RENDER_ORDER)
            if set(full_by_label) == set(RENDER_ORDER)
            and set(by_label) == set(RENDER_ORDER)
            else set()
        )
        if set(mode["oracle_agreements"]) != expected_labels:
            raise ScreenError(f"modes.{mode_name}.oracle_agreements shape mismatch")
        for label in expected_labels:
            _validate_comparison(
                mode["oracle_agreements"][label],
                by_label[label],
                full_by_label[label],
                f"modes.{mode_name}.oracle_agreements.{label}",
            )
    recomputed = evaluate_invalidation(modes)
    if report["decision"] != recomputed:
        raise ScreenError("stored decision disagrees with render hashes")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--frame", type=int, required=True)
    parser.add_argument("--samples", type=int, required=True)
    parser.add_argument("--device", choices=("METAL", "CPU"), default="METAL")
    args = parser.parse_args(argv)
    if not 1 <= args.width <= MAX_DIMENSION or not 1 <= args.height <= MAX_DIMENSION:
        parser.error("width and height must be in [1,4096]")
    if args.width * args.height > MAX_PIXELS:
        parser.error("pixel count exceeds the bounded screen")
    if not 1 <= args.samples <= MAX_SAMPLES:
        parser.error("samples must be in [1,4096]")
    if not 0 <= args.frame <= 1_000_000:
        parser.error("frame must be in [0,1000000]")
    return args


def _prepare_output_dir(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.exists():
        if resolved.is_symlink() or not resolved.is_dir():
            raise ScreenError("output path must be a non-symlink directory")
        if any(resolved.iterdir()):
            raise ScreenError("output directory must be empty")
    else:
        resolved.mkdir(mode=0o700, parents=True)
    return resolved


def _configure_device(bpy: Any, scene: Any, requested: str) -> tuple[str, list[str]]:
    if requested == "CPU":
        scene.cycles.device = "CPU"
        return "CPU", ["CPU"]
    preferences = bpy.context.preferences.addons["cycles"].preferences
    preferences.compute_device_type = "METAL"
    preferences.get_devices()
    if hasattr(preferences, "get_devices_for_type"):
        preferences.get_devices_for_type("METAL")
    metal = [
        device
        for device in preferences.devices
        if getattr(device, "type", "") == "METAL"
    ]
    if not metal:
        raise ScreenError("METAL requested but Cycles enumerated no Metal device")
    for device in preferences.devices:
        device.use = device in metal
    scene.cycles.device = "GPU"
    return "GPU/METAL", sorted(str(device.name) for device in metal)


def _apply_tag(scene: Any, mode: str) -> str:
    if mode == "none":
        return "no_explicit_id_tag"
    if mode == "time":
        scene.update_tag(refresh={"TIME"})
        return "scene.update_tag(refresh={'TIME'})"
    if mode == "full":
        scene.update_tag()
        return "scene.update_tag()"
    raise ScreenError(f"unknown invalidation mode {mode!r}")


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    encoded = (
        json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _run_blender(args: argparse.Namespace) -> dict[str, Any]:
    try:
        import bpy  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - Blender runtime boundary
        raise ScreenError("this experimental harness must run inside Blender") from exc

    source = args.scene.expanduser().resolve(strict=True)
    if not source.is_file() or source.is_symlink():
        raise ScreenError("scene must be a regular non-symlink file")
    scene_sha_before, scene_bytes = _sha256_file(source)
    output_dir = _prepare_output_dir(args.output_dir)
    current = Path(bpy.data.filepath).resolve() if bpy.data.filepath else None
    if current != source:
        bpy.ops.wm.open_mainfile(filepath=str(source))
    if Path(bpy.data.filepath).resolve(strict=True) != source:
        raise ScreenError("Blender did not open the requested scene")
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    native_integrator = {
        key: int(getattr(scene.cycles, key)) for key in INTEGRATOR_KEYS
    }
    scene.cycles.use_animated_seed = False
    scene.cycles.use_adaptive_sampling = False
    scene.cycles.use_denoising = False
    scene.cycles.pixel_filter_type = "BLACKMAN_HARRIS"
    scene.cycles.filter_width = 1.5
    scene.render.use_persistent_data = True
    scene.render.filter_size = 1.5
    scene.render.resolution_x = args.width
    scene.render.resolution_y = args.height
    scene.render.resolution_percentage = 100
    scene.render.use_border = False
    scene.render.use_crop_to_border = False
    scene.render.use_file_extension = True
    scene.render.use_overwrite = True
    scene.render.use_multiview = False
    scene.render.use_compositing = False
    scene.render.use_sequencer = False
    scene.render.use_freestyle = False
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.image_settings.color_depth = "8"
    scene.render.image_settings.compression = 0
    actual_device, enabled_devices = _configure_device(bpy, scene, args.device)
    reference_sampling = {
        "adaptive_min_samples": int(scene.cycles.adaptive_min_samples),
        "adaptive_threshold": float(scene.cycles.adaptive_threshold),
        "use_adaptive_sampling": False,
        "use_light_tree": bool(scene.cycles.use_light_tree),
    }
    scene.frame_set(args.frame)

    seed_a = int(scene_sha_before[:8], 16) & 0x7FFFFFFF
    seed_b = (seed_a ^ 0x5A5A5A5A) & 0x7FFFFFFF
    configs = {
        "a": {
            "sample_offset": 0,
            "sample_range": [0, args.samples],
            "samples": args.samples,
            "seed": seed_a,
        },
        "b": {
            "sample_offset": args.samples,
            "sample_range": [args.samples, 2 * args.samples],
            "samples": args.samples,
            "seed": seed_b,
        },
    }

    decoded_cache: dict[tuple[str, str], bytes] = {}

    def render_once(mode: str, label: str, config_name: str) -> dict[str, Any]:
        mode_spec = MODE_SPECS[mode]
        config = configs[config_name]
        destination = output_dir / f"{mode}-{label}.png"
        if destination.exists():
            raise ScreenError("render destination unexpectedly exists")
        started = time.perf_counter()
        if mode_spec["frame_set_per_render"]:
            scene.frame_set(args.frame)
        elif int(scene.frame_current) != args.frame:
            raise ScreenError("no-frame-set mode observed an unexpected current frame")
        for key, value in native_integrator.items():
            setattr(scene.cycles, key, value)
        scene.cycles.use_light_tree = reference_sampling["use_light_tree"]
        scene.cycles.use_adaptive_sampling = False
        scene.cycles.adaptive_min_samples = reference_sampling["adaptive_min_samples"]
        scene.cycles.adaptive_threshold = reference_sampling["adaptive_threshold"]
        scene.cycles.samples = config["samples"]
        scene.cycles.seed = config["seed"]
        scene.cycles.sample_offset = config["sample_offset"]
        scene.render.resolution_x = args.width
        scene.render.resolution_y = args.height
        scene.render.filepath = str(destination)
        _apply_tag(scene, mode_spec["tag_mode"])
        bpy.context.view_layer.update()
        bpy.ops.render.render(write_still=True)
        wall_s = time.perf_counter() - started
        digest, byte_count = _sha256_file(destination)
        rgb = _decode_png_rgb(
            destination,
            expected_width=args.width,
            expected_height=args.height,
        )
        decoded_cache[(mode, label)] = rgb
        return {
            "bytes": byte_count,
            "config": config_name,
            "decoded_rgb_sha256": hashlib.sha256(rgb).hexdigest(),
            "height": args.height,
            "label": label,
            "path": destination.name,
            "sample_offset": config["sample_offset"],
            "sample_range": config["sample_range"],
            "samples": config["samples"],
            "seed": config["seed"],
            "sha256": digest,
            "wall_s": _round9(wall_s),
            "width": args.width,
        }

    warmup = render_once("full", "warmup", "a")
    modes: dict[str, Any] = {}
    for mode in MODE_ORDER:
        mode_spec = MODE_SPECS[mode]
        records: list[dict[str, Any]] = []
        error: str | None = None
        supported = True
        action = {
            "full": "scene.update_tag()",
            "none": "no_explicit_id_tag",
            "time": "scene.update_tag(refresh={'TIME'})",
        }[mode_spec["tag_mode"]]
        frame_action = (
            "scene.frame_set(requested_frame)"
            if mode_spec["frame_set_per_render"]
            else "assert_same_frame_without_frame_set"
        )
        try:
            for label, config_name in (("a1", "a"), ("b", "b"), ("a2", "a")):
                records.append(render_once(mode, label, config_name))
        except BaseException as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
            if mode_spec["tag_mode"] == "time" and not records:
                supported = False
        comparisons: dict[str, Any] = {}
        by_label = {record["label"]: record for record in records}
        for left_label, right_label in (("a1", "a2"), ("a1", "b"), ("b", "a2")):
            if left_label in by_label and right_label in by_label:
                left = by_label[left_label]
                right = by_label[right_label]
                stats = _rgb_difference_stats(
                    decoded_cache[(mode, left_label)],
                    decoded_cache[(mode, right_label)],
                )
                comparisons[f"{left_label}_vs_{right_label}"] = _comparison(
                    left, right, stats
                )
        modes[mode] = {
            "error": error,
            "frame_action": frame_action,
            "frame_set_per_render": mode_spec["frame_set_per_render"],
            "mode": mode,
            "oracle_agreements": {},
            "pair_agreements": comparisons,
            "renders": records,
            "supported": supported,
            "tag_action": action,
        }

    full_by_label = {
        record["label"]: record for record in modes["full"]["renders"]
    }
    if set(full_by_label) == set(RENDER_ORDER):
        for mode in MODE_ORDER:
            by_label = {
                record["label"]: record for record in modes[mode]["renders"]
            }
            if set(by_label) != set(RENDER_ORDER):
                continue
            for label in RENDER_ORDER:
                stats = _rgb_difference_stats(
                    decoded_cache[(mode, label)],
                    decoded_cache[("full", label)],
                )
                modes[mode]["oracle_agreements"][label] = _comparison(
                    by_label[label], full_by_label[label], stats
                )

    decision = evaluate_invalidation(modes)
    scene_sha_after, scene_bytes_after = _sha256_file(source)
    if scene_sha_after != scene_sha_before or scene_bytes_after != scene_bytes:
        raise ScreenError("source scene changed during the read-only screen")
    build_hash = bpy.app.build_hash
    if isinstance(build_hash, bytes):
        build_hash = build_hash.decode("ascii", errors="replace")
    report = {
        "blender": {
            "build_hash": str(build_hash),
            "version": str(bpy.app.version_string),
        },
        "configuration": {
            "compositing": False,
            "configs": configs,
            "device": actual_device,
            "enabled_device_names": enabled_devices,
            "frame": args.frame,
            "height": args.height,
            "mode_execution_order": list(MODE_ORDER),
            "native_integrator": native_integrator,
            "persistent_data": True,
            "pixel_filter": {"type": "BLACKMAN_HARRIS", "width": 1.5},
            "png_compression": 0,
            "reference_sampling": reference_sampling,
            "render_sequence_per_mode": ["a1", "b", "a2"],
            "samples": args.samples,
            "semantic_equivalence": {
                "agreement_min": SEMANTIC_AGREEMENT_MIN,
                "changed_pixel_fraction_max": SEMANTIC_CHANGED_PIXEL_FRACTION_MAX,
                "exact_decoded_hash_retained_as_diagnostic": True,
            },
            "independence_observability": {
                "agreement_max": INDEPENDENCE_AGREEMENT_MAX,
                "changed_pixel_fraction_min": INDEPENDENCE_CHANGED_PIXEL_FRACTION_MIN,
            },
            "width": args.width,
        },
        "decision": decision,
        "experimental_only": True,
        "host": {
            "machine": platform.machine(),
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "kind": KIND,
        "limitations": [
            "one local scene/frame/host trial cannot authorize a backend invalidation change",
            "decoded RGB equality does not establish cross-scene, animated, "
            "compositor, volume, or motion-blur safety",
            "a narrower mode requires repeated held-out scene coverage before production use",
        ],
        "modes": modes,
        "scene": {
            "bytes": scene_bytes,
            "path": str(source),
            "sha256": scene_sha_before,
        },
        "schema_version": SCHEMA_VERSION,
        "warmup": warmup,
    }
    validate_report(report)
    _write_json_atomic(output_dir / "resident-invalidation-screen.json", report)
    return report


def _cli_argv(argv: list[str]) -> list[str]:
    if "--" in argv:
        return argv[argv.index("--") + 1 :]
    return argv[1:]


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv if argv is None else argv
    try:
        args = _parse_args(_cli_argv(raw))
        report = _run_blender(args)
    except BaseException as exc:
        envelope = {
            "error": f"{type(exc).__name__}: {exc}"[:1000],
            "kind": KIND,
            "ok": False,
            "schema_version": SCHEMA_VERSION,
        }
        print(json.dumps(envelope, sort_keys=True), file=sys.stderr, flush=True)
        return 2
    print(
        json.dumps(
            {
                "decision": report["decision"],
                "kind": KIND,
                "ok": True,
                "report": "resident-invalidation-screen.json",
                "schema_version": SCHEMA_VERSION,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
