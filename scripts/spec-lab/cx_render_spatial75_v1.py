#!/usr/bin/env python3
"""Pinned 75%-resolution render postprocess and legacy L1 gate experiment.

This module is deliberately narrow.  It accepts only 810x1440 RGB/RGBA 8-bit
PNGs and expands a gate-selected draft to a 1080x1920 RGBA 8-bit PNG with
Pillow's BICUBIC RGB and alpha operators.  It does not make a quality
claim: the included draft/verify gate is an exact partition-reduction
implementation of the existing reference-free RGB L1 gate, not a
reference-backed quality audit.

The public ``benchmark_gate_and_postprocess`` path decodes the draft once.  Its
decoded RGB and alpha planes are retained by ``GateResult`` and reused by the
postprocessor.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field, replace
from functools import lru_cache
import hashlib
from io import BytesIO
import importlib
import json
import math
import os
from pathlib import Path
import secrets
import stat
import struct
import sys
import time
from typing import Any, Iterable, Sequence
import zlib

import numpy as np
import PIL
from PIL import Image, UnidentifiedImageError


POLICY_ID = "cx-render-spatial75-v1"
SCHEMA_VERSION = 1
RESULT_KIND = "cx_render_spatial75_v1_result"

INPUT_SIZE = (810, 1440)
OUTPUT_SIZE = (1080, 1920)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 32 * 1024 * 1024

AGREEMENT_MIN_TILE_EDGE = 32
AGREEMENT_MAX_LONG_EDGE_TILES = 16
MICROTILE_EDGE = 32
MAX_AGREEMENT_TILES = 8_192
GLOBAL_AGREEMENT_MIN = 0.90
WORST_TILE_AGREEMENT_MIN = 0.85
MICROTILE_AGREEMENT_MIN = 0.70

# Pillow's enum is part of the frozen operator, not merely descriptive output.
PILLOW_BICUBIC_ENUM = 3
PNG_COMPRESSION_LEVEL = 0
PNG_OPTIMIZE = False


POLICY_DESCRIPTOR: dict[str, Any] = {
    "id": POLICY_ID,
    "schema_version": SCHEMA_VERSION,
    "experimental": True,
    "quality_claim": False,
    "input": {
        "media_type": "image/png",
        "dimensions": list(INPUT_SIZE),
        "bit_depth": 8,
        "color_types": ["RGB", "RGBA"],
    },
    "upstream_sampling": {
        "draft_samples": 4,
        "verify_samples": 4,
        "disjoint_sample_ranges_required": True,
        "independent_seeds_required": True,
        "binding": "enforced by the render backend; not inferable from PNG bytes",
    },
    "output": {
        "media_type": "image/png",
        "dimensions": list(OUTPUT_SIZE),
        "bit_depth": 8,
        "mode": "RGBA",
        "compression_level": PNG_COMPRESSION_LEVEL,
        "optimize": PNG_OPTIMIZE,
        "publication": "same-directory atomic hard-link, no-clobber",
    },
    "pipeline": {
        "prepared_bytes_maximum": MAX_OUTPUT_BYTES,
        "prepared_representation": "immutable validated PNG bytes",
        "preparation_authorizes_publication": False,
        "publication_requires_bound_gate_pass": True,
        "prepared_and_gate_draft_identity_must_match": True,
        "policy_runtime_and_operator_revalidated_before_publication": True,
        "fused_authorization": {
            "prepared_output": "in-process immutable object-identity seal",
            "decoded_identity_hashes_per_call": 2,
            "prepared_png_rehashes_per_call": 0,
            "gate_round_trip_revalidation": False,
            "rejection_publishes": False,
        },
        "retained_backend_handoff": {
            "png_bytes": "immutable exact bytes with SHA-256 and byte-count claims",
            "decoded_pixels": "immutable decoder-attested bytes with SHA-256 claim",
            "spatial_redecode": False,
            "spatial_strict_png_container_validation": True,
        },
    },
    "transform": {
        "rgb_representation": "straight (not alpha-premultiplied)",
        "rgb_operator": "PIL.Image.Resampling.BICUBIC",
        "rgb_operator_enum": PILLOW_BICUBIC_ENUM,
        "alpha_operator": "PIL.Image.Resampling.BICUBIC",
        "alpha_operator_enum": PILLOW_BICUBIC_ENUM,
        "post_filter": None,
    },
    "reference_free_gate": {
        "metric": "one_minus_mean_absolute_rgb_difference",
        "alpha_ignored": True,
        "implementation": "numpy uint64 exact partition reductions",
        "regional_grid": {
            "max_long_edge_tiles": AGREEMENT_MAX_LONG_EDGE_TILES,
            "minimum_nominal_edge_pixels": AGREEMENT_MIN_TILE_EDGE,
        },
        "microtile_grid": {"nominal_edge_pixels": MICROTILE_EDGE},
        "thresholds": {
            "global_minimum": GLOBAL_AGREEMENT_MIN,
            "regional_worst_minimum": WORST_TILE_AGREEMENT_MIN,
            "microtile_worst_minimum": MICROTILE_AGREEMENT_MIN,
        },
        "quality_authorization": False,
    },
}


class Spatial75Error(ValueError):
    """Stable-code policy or artifact error."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class GateRejected(Spatial75Error):
    """The reference-free draft/verify gate did not select the draft."""

    def __init__(self) -> None:
        super().__init__("draft_verify_gate_rejected")


@dataclass(frozen=True)
class DecodedPng:
    """Strictly decoded source planes retained for deterministic reuse."""

    rgb: np.ndarray
    alpha: np.ndarray
    mode: str
    sha256: str
    byte_count: int


@dataclass(frozen=True)
class GateResult:
    """Exact legacy agreement values plus the retained decoded draft."""

    global_score: float
    regional_worst: float
    tiles: list[dict[str, Any]]
    microtile_worst: float
    microtile_count: int
    passed: bool
    decoded_draft: DecodedPng
    decoded_verify: DecodedPng
    timings_ns: dict[str, int]
    draft_identity_sha256: str
    verify_identity_sha256: str
    policy_sha256: str
    runtime_sha256: str
    binding_sha256: str

    def legacy_tuple(
        self,
    ) -> tuple[float, float, list[dict[str, Any]], float, int]:
        """Return the exact shape of the backend's current ``_agreement``."""

        return (
            self.global_score,
            self.regional_worst,
            self.tiles,
            self.microtile_worst,
            self.microtile_count,
        )

    def receipt(self) -> dict[str, Any]:
        return {
            "policy_id": POLICY_ID,
            "policy_sha256": self.policy_sha256,
            "metric": "one_minus_mean_absolute_rgb_difference",
            "global_agreement": self.global_score,
            "worst_tile_agreement": self.regional_worst,
            "tiles": [
                {"rect": list(tile["rect"]), "score": tile["score"]}
                for tile in self.tiles
            ],
            "worst_microtile_agreement": self.microtile_worst,
            "microtile_count": self.microtile_count,
            "thresholds": {
                "global_minimum": GLOBAL_AGREEMENT_MIN,
                "regional_worst_minimum": WORST_TILE_AGREEMENT_MIN,
                "microtile_worst_minimum": MICROTILE_AGREEMENT_MIN,
            },
            "passed": self.passed,
            "draft": {
                "sha256": self.decoded_draft.sha256,
                "decoded_identity_sha256": self.draft_identity_sha256,
                "bytes": self.decoded_draft.byte_count,
                "mode": self.decoded_draft.mode,
            },
            "verify": {
                "sha256": self.decoded_verify.sha256,
                "decoded_identity_sha256": self.verify_identity_sha256,
                "bytes": self.decoded_verify.byte_count,
                "mode": self.decoded_verify.mode,
            },
            "timings_ns": dict(self.timings_ns),
            "runtime_sha256": self.runtime_sha256,
            "binding_sha256": self.binding_sha256,
            "runtime": runtime_identity(),
        }


@dataclass(frozen=True, slots=True)
class PreparedTimings:
    """Immutable preparation-stage timing record."""

    identity: int
    transform: int
    encode: int
    validate: int
    bind: int
    total: int

    def as_dict(self) -> dict[str, int]:
        return {
            "identity": self.identity,
            "transform": self.transform,
            "encode": self.encode,
            "validate": self.validate,
            "bind": self.bind,
            "total": self.total,
        }


@dataclass(frozen=True, slots=True)
class _PreparedSeal:
    """In-process immutable provenance for hash-free fused authorization."""

    encoded_png: bytes = field(repr=False)
    input_sha256: str
    input_decoded_identity_sha256: str
    input_mode: str
    input_bytes: int
    output_sha256: str
    output_bytes: int
    policy_sha256: str
    runtime_sha256: str
    binding_sha256: str
    timings_ns: PreparedTimings


@dataclass(frozen=True, slots=True)
class PreparedOutput:
    """Immutable, validated output bytes awaiting gate authorization."""

    encoded_png: bytes
    input_sha256: str
    input_decoded_identity_sha256: str
    input_mode: str
    input_bytes: int
    output_sha256: str
    output_bytes: int
    policy_sha256: str
    runtime_sha256: str
    binding_sha256: str
    timings_ns: PreparedTimings
    _seal: _PreparedSeal | None = field(repr=False, compare=False)

    def receipt(self) -> dict[str, Any]:
        return {
            "policy_id": POLICY_ID,
            "policy_sha256": self.policy_sha256,
            "experimental": True,
            "quality_claim": False,
            "publication_authorized": False,
            "input": {
                "sha256": self.input_sha256,
                "decoded_identity_sha256": self.input_decoded_identity_sha256,
                "mode": self.input_mode,
                "bytes": self.input_bytes,
            },
            "prepared_output": {
                "sha256": self.output_sha256,
                "bytes": self.output_bytes,
                "dimensions": list(OUTPUT_SIZE),
                "mode": "RGBA",
                "encoded_bytes_held_in_memory": True,
            },
            "runtime_sha256": self.runtime_sha256,
            "binding_sha256": self.binding_sha256,
            "timings_ns": self.timings_ns.as_dict(),
        }


@dataclass(frozen=True)
class PostprocessResult:
    output_path: Path
    input_sha256: str
    output_sha256: str
    output_bytes: int
    timings_ns: dict[str, int]
    input_decode_reused: bool

    def receipt(self) -> dict[str, Any]:
        return {
            "policy_id": POLICY_ID,
            "policy_sha256": _sha256_bytes(_canonical_json(POLICY_DESCRIPTOR)),
            "experimental": True,
            "quality_claim": False,
            "input": {"sha256": self.input_sha256},
            "output": {
                "path": str(self.output_path),
                "sha256": self.output_sha256,
                "bytes": self.output_bytes,
                "dimensions": list(OUTPUT_SIZE),
                "mode": "RGBA",
            },
            "operators": dict(POLICY_DESCRIPTOR["transform"]),
            "encoding": {
                "format": "PNG",
                "compression_level": PNG_COMPRESSION_LEVEL,
                "optimize": PNG_OPTIMIZE,
            },
            "input_decode_reused": self.input_decode_reused,
            "timings_ns": dict(self.timings_ns),
            "runtime": runtime_identity(),
        }


@dataclass(frozen=True)
class FusedGatePublishResult:
    gate: GateResult
    postprocess: PostprocessResult
    timings_ns: dict[str, int]


@dataclass(frozen=True)
class BenchmarkResult:
    gate: GateResult
    postprocess: PostprocessResult
    timings_ns: dict[str, int]

    def receipt(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": RESULT_KIND,
            "policy": POLICY_DESCRIPTOR,
            "policy_sha256": _sha256_bytes(_canonical_json(POLICY_DESCRIPTOR)),
            "gate": self.gate.receipt(),
            "postprocess": self.postprocess.receipt(),
            "timings_ns": dict(self.timings_ns),
            "runtime": runtime_identity(),
        }


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _compiled_module_record(module_name: str) -> dict[str, str | None]:
    try:
        module = importlib.import_module(module_name)
        source = getattr(module, "__file__", None)
        if not source:
            return {"file": None, "sha256": None}
        path = Path(source).resolve()
        if not path.is_file():
            return {"file": path.name, "sha256": None}
        return {"file": path.name, "sha256": _sha256_file(path)}
    except (ImportError, OSError):
        return {"file": None, "sha256": None}


@lru_cache(maxsize=1)
def runtime_identity() -> dict[str, Any]:
    """Pin the Python packages and compiled operators used by the policy."""

    compiled = {
        "PIL._imaging": _compiled_module_record("PIL._imaging"),
        "numpy._core._multiarray_umath": _compiled_module_record(
            "numpy._core._multiarray_umath"
        ),
    }
    versions = {"pillow": PIL.__version__, "numpy": np.__version__}
    operators = {
        "PIL.Image.Resampling.BICUBIC": int(Image.Resampling.BICUBIC),
        "expected_bicubic_enum": PILLOW_BICUBIC_ENUM,
    }
    tree = {
        "versions": versions,
        "compiled_modules": compiled,
        "operators": operators,
    }
    return {
        **tree,
        "dependency_tree_sha256": _sha256_bytes(_canonical_json(tree)),
        "module_sha256": _sha256_file(Path(__file__).resolve()),
    }


def _policy_sha256() -> str:
    return _sha256_bytes(_canonical_json(POLICY_DESCRIPTOR))


def _runtime_sha256() -> str:
    return _sha256_bytes(_canonical_json(runtime_identity()))


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _decoded_identity_sha256(decoded: DecodedPng) -> str:
    """Bind strict source metadata and the exact retained RGB/alpha planes."""

    if type(decoded) is not DecodedPng:
        raise Spatial75Error("decoded_input_type_mismatch")
    if (
        decoded.mode not in {"RGB", "RGBA"}
        or not _is_sha256(decoded.sha256)
        or not 1 <= decoded.byte_count <= MAX_INPUT_BYTES
        or decoded.rgb.dtype != np.uint8
        or decoded.alpha.dtype != np.uint8
        or decoded.rgb.shape != (INPUT_SIZE[1], INPUT_SIZE[0], 3)
        or decoded.alpha.shape != (INPUT_SIZE[1], INPUT_SIZE[0])
        or not decoded.rgb.flags.c_contiguous
        or not decoded.alpha.flags.c_contiguous
        or decoded.rgb.flags.writeable
        or decoded.alpha.flags.writeable
    ):
        raise Spatial75Error("decoded_input_identity_mismatch")
    digest = hashlib.sha256()
    digest.update(b"cx-render-spatial75-decoded-v1\0")
    digest.update(
        _canonical_json(
            {
                "source_sha256": decoded.sha256,
                "source_bytes": decoded.byte_count,
                "mode": decoded.mode,
                "size": list(INPUT_SIZE),
                "rgb_shape": list(decoded.rgb.shape),
                "alpha_shape": list(decoded.alpha.shape),
                "dtype": "uint8",
            }
        )
    )
    digest.update(memoryview(decoded.rgb).cast("B"))
    digest.update(memoryview(decoded.alpha).cast("B"))
    return digest.hexdigest()


def _prepared_binding_sha256(prepared: PreparedOutput) -> str:
    return _sha256_bytes(
        _canonical_json(
            {
                "kind": "cx_render_spatial75_prepared_output_v1",
                "input_sha256": prepared.input_sha256,
                "input_decoded_identity_sha256": (
                    prepared.input_decoded_identity_sha256
                ),
                "input_mode": prepared.input_mode,
                "input_bytes": prepared.input_bytes,
                "output_sha256": prepared.output_sha256,
                "output_bytes": prepared.output_bytes,
                "policy_sha256": prepared.policy_sha256,
                "runtime_sha256": prepared.runtime_sha256,
            }
        )
    )


def _gate_binding_sha256(gate: GateResult) -> str:
    return _sha256_bytes(
        _canonical_json(
            {
                "kind": "cx_render_spatial75_gate_v1",
                "global_score": gate.global_score,
                "regional_worst": gate.regional_worst,
                "tiles": gate.tiles,
                "microtile_worst": gate.microtile_worst,
                "microtile_count": gate.microtile_count,
                "passed": gate.passed,
                "draft_sha256": gate.decoded_draft.sha256,
                "verify_sha256": gate.decoded_verify.sha256,
                "draft_identity_sha256": gate.draft_identity_sha256,
                "verify_identity_sha256": gate.verify_identity_sha256,
                "policy_sha256": gate.policy_sha256,
                "runtime_sha256": gate.runtime_sha256,
            }
        )
    )


def _validate_prepared_output(prepared: PreparedOutput) -> None:
    _validate_prepared_seal(prepared)
    if _sha256_bytes(prepared.encoded_png) != prepared.output_sha256:
        raise Spatial75Error("prepared_output_identity_mismatch")
    if _prepared_binding_sha256(prepared) != prepared.binding_sha256:
        raise Spatial75Error("prepared_binding_mismatch")


def _validate_prepared_seal(prepared: PreparedOutput) -> None:
    """O(1) provenance validation for the fused in-process publication path."""

    _assert_operator_pins()
    if type(prepared) is not PreparedOutput:
        raise Spatial75Error("prepared_output_type_mismatch")
    if type(prepared.timings_ns) is not PreparedTimings:
        raise Spatial75Error("prepared_output_identity_mismatch")
    seal = prepared._seal
    if type(seal) is not _PreparedSeal:
        raise Spatial75Error("prepared_output_identity_mismatch")
    timing_values = prepared.timings_ns.as_dict()
    if (
        type(prepared.encoded_png) is not bytes
        or not 1 <= len(prepared.encoded_png) <= MAX_OUTPUT_BYTES
        or prepared.output_bytes != len(prepared.encoded_png)
        or not _is_sha256(prepared.output_sha256)
        or not _is_sha256(prepared.input_sha256)
        or not _is_sha256(prepared.input_decoded_identity_sha256)
        or prepared.input_mode not in {"RGB", "RGBA"}
        or not 1 <= prepared.input_bytes <= MAX_INPUT_BYTES
        or any(type(value) is not int or value < 0 for value in timing_values.values())
        or prepared.timings_ns.total
        < sum(
            timing_values[name]
            for name in ("identity", "transform", "encode", "validate", "bind")
        )
    ):
        raise Spatial75Error("prepared_output_identity_mismatch")
    if prepared.policy_sha256 != _policy_sha256():
        raise Spatial75Error("prepared_policy_identity_mismatch")
    if prepared.runtime_sha256 != _runtime_sha256():
        raise Spatial75Error("prepared_runtime_identity_mismatch")
    if (
        not _is_sha256(prepared.binding_sha256)
        or prepared.encoded_png is not seal.encoded_png
        or prepared.input_sha256 != seal.input_sha256
        or prepared.input_decoded_identity_sha256
        != seal.input_decoded_identity_sha256
        or prepared.input_mode != seal.input_mode
        or prepared.input_bytes != seal.input_bytes
        or prepared.output_sha256 != seal.output_sha256
        or prepared.output_bytes != seal.output_bytes
        or prepared.policy_sha256 != seal.policy_sha256
        or prepared.runtime_sha256 != seal.runtime_sha256
        or prepared.binding_sha256 != seal.binding_sha256
        or prepared.timings_ns is not seal.timings_ns
    ):
        raise Spatial75Error("prepared_output_identity_mismatch")


def _validate_gate_result(gate: GateResult) -> None:
    _assert_operator_pins()
    if type(gate) is not GateResult:
        raise Spatial75Error("gate_result_type_mismatch")
    draft_identity = _decoded_identity_sha256(gate.decoded_draft)
    verify_identity = _decoded_identity_sha256(gate.decoded_verify)
    metric_values = (
        gate.global_score,
        gate.regional_worst,
        gate.microtile_worst,
    )
    expected_rectangles = list(
        _rectangles(INPUT_SIZE, *_agreement_grid(INPUT_SIZE))
    )
    if (
        draft_identity != gate.draft_identity_sha256
        or verify_identity != gate.verify_identity_sha256
        or gate.policy_sha256 != _policy_sha256()
        or gate.runtime_sha256 != _runtime_sha256()
        or type(gate.passed) is not bool
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0.0 <= float(value) <= 1.0
            for value in metric_values
        )
        or gate.microtile_count
        != _microtile_grid(INPUT_SIZE)[0] * _microtile_grid(INPUT_SIZE)[1]
        or len(gate.tiles) != len(expected_rectangles)
    ):
        raise Spatial75Error("gate_result_identity_mismatch")
    tile_scores: list[float] = []
    for tile, expected_rect in zip(gate.tiles, expected_rectangles, strict=True):
        if (
            not isinstance(tile, dict)
            or set(tile) != {"rect", "score"}
            or tile["rect"] != expected_rect
            or isinstance(tile["score"], bool)
            or not isinstance(tile["score"], (int, float))
            or not math.isfinite(float(tile["score"]))
            or not 0.0 <= float(tile["score"]) <= 1.0
        ):
            raise Spatial75Error("gate_result_tile_identity_mismatch")
        tile_scores.append(float(tile["score"]))
    if gate.regional_worst != min(tile_scores):
        raise Spatial75Error("gate_result_regional_worst_mismatch")
    metric_pass = (
        gate.global_score >= GLOBAL_AGREEMENT_MIN
        and gate.regional_worst >= WORST_TILE_AGREEMENT_MIN
        and gate.microtile_worst >= MICROTILE_AGREEMENT_MIN
    )
    if metric_pass is not gate.passed:
        raise Spatial75Error("gate_result_decision_mismatch")
    if (
        not _is_sha256(gate.binding_sha256)
        or _gate_binding_sha256(gate) != gate.binding_sha256
    ):
        raise Spatial75Error("gate_result_binding_mismatch")


def _assert_operator_pins() -> None:
    if (
        Image.Resampling.BICUBIC.name != "BICUBIC"
        or int(Image.Resampling.BICUBIC) != PILLOW_BICUBIC_ENUM
    ):
        raise Spatial75Error("pillow_resampling_operator_mismatch")


def _read_bounded(path: str | Path, maximum: int) -> tuple[bytes, int]:
    started = time.perf_counter_ns()
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise Spatial75Error("unreadable_png") from exc
    elapsed = time.perf_counter_ns() - started
    if not 1 <= len(data) <= maximum:
        raise Spatial75Error("png_size_out_of_bounds")
    return data, elapsed


def _validate_png_container(
    data: bytes,
    expected_size: tuple[int, int],
    *,
    required_mode: str | None = None,
    collect_idat: bool = True,
) -> tuple[str, bytes]:
    """Validate a single-frame RGB/RGBA8 PNG and return mode and IDAT bytes."""

    if not data.startswith(PNG_SIGNATURE):
        raise Spatial75Error("invalid_png_signature")
    offset = len(PNG_SIGNATURE)
    chunk_index = 0
    saw_ihdr = False
    saw_idat = False
    saw_iend = False
    idat_ended = False
    mode: str | None = None
    compressed = bytearray()
    while offset < len(data):
        if len(data) - offset < 12:
            raise Spatial75Error("truncated_png_chunk")
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise Spatial75Error("truncated_png_chunk")
        payload = data[offset + 8 : offset + 8 + length]
        if not all(
            65 <= value <= 90 or 97 <= value <= 122 for value in chunk_type
        ):
            raise Spatial75Error("invalid_png_chunk_type")
        if chunk_type[2] & 0x20:
            raise Spatial75Error("invalid_png_chunk_reserved_bit")
        expected_crc = struct.unpack(">I", data[offset + 8 + length : end])[0]
        actual_crc = zlib.crc32(payload, zlib.crc32(chunk_type)) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise Spatial75Error("png_crc_mismatch")
        if chunk_index == 0 and chunk_type != b"IHDR":
            raise Spatial75Error("png_ihdr_not_first")
        if chunk_type == b"IHDR":
            if saw_ihdr or length != 13:
                raise Spatial75Error("invalid_png_ihdr")
            width, height, depth, color, compression, filtering, interlace = (
                struct.unpack(">IIBBBBB", payload)
            )
            if (width, height) != expected_size:
                raise Spatial75Error("png_dimension_mismatch")
            if depth != 8:
                raise Spatial75Error("png_bit_depth_not_8")
            if color not in (2, 6):
                raise Spatial75Error("png_color_type_not_rgb_or_rgba")
            if compression != 0 or filtering != 0 or interlace not in (0, 1):
                raise Spatial75Error("unsupported_png_method")
            mode = "RGB" if color == 2 else "RGBA"
            if required_mode is not None and mode != required_mode:
                raise Spatial75Error("png_mode_mismatch")
            saw_ihdr = True
        elif chunk_type == b"IDAT":
            if not saw_ihdr or saw_iend or idat_ended:
                raise Spatial75Error("invalid_png_idat_order")
            saw_idat = True
            if collect_idat:
                compressed.extend(payload)
        elif chunk_type == b"IEND":
            if length != 0 or not saw_idat or saw_iend:
                raise Spatial75Error("invalid_png_iend")
            saw_iend = True
            if end != len(data):
                raise Spatial75Error("png_trailing_data")
        elif chunk_type == b"PLTE":
            if saw_idat or saw_iend:
                raise Spatial75Error("invalid_png_plte_order")
        else:
            if saw_idat:
                idat_ended = True
            if (
                chunk_type[:1].isalpha()
                and chunk_type[:1].isupper()
            ):
                raise Spatial75Error("unknown_png_critical_chunk")
        offset = end
        chunk_index += 1
        if saw_iend:
            break
    if not (saw_ihdr and saw_idat and saw_iend) or mode is None:
        raise Spatial75Error("incomplete_png")
    return mode, bytes(compressed)


def _decode_png_bytes(data: bytes, expected_size: tuple[int, int]) -> DecodedPng:
    mode, _compressed = _validate_png_container(
        data, expected_size, collect_idat=False
    )
    return _decode_validated_png_bytes(data, expected_size, mode)


def _pillow_decode_validated_png(
    data: bytes,
    expected_size: tuple[int, int],
    mode: str,
    *,
    capture_pixels: bool,
) -> np.ndarray | None:
    """Run Pillow's full decoder after strict container validation.

    Output validation needs the full decoder to consume and verify the zlib
    stream, but does not need another materialized pixel copy.  Gate inputs set
    ``capture_pixels`` so their one decode can be retained for scoring and
    postprocessing.
    """

    try:
        with Image.open(BytesIO(data)) as source:
            if (
                source.format != "PNG"
                or source.mode != mode
                or source.size != expected_size
                or getattr(source, "n_frames", 1) != 1
            ):
                raise Spatial75Error("png_decoder_identity_mismatch")
            source.load()
            if capture_pixels:
                return np.array(source, dtype=np.uint8, copy=True)
            return None
    except Spatial75Error:
        raise
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise Spatial75Error("png_decode_failed") from exc


def _decode_validated_png_bytes(
    data: bytes, expected_size: tuple[int, int], mode: str
) -> DecodedPng:
    decoded = _pillow_decode_validated_png(
        data, expected_size, mode, capture_pixels=True
    )
    if decoded is None:
        raise Spatial75Error("decoded_pixels_unavailable")
    width, height = expected_size
    channels = 3 if mode == "RGB" else 4
    if decoded.shape != (height, width, channels):
        raise Spatial75Error("decoded_shape_mismatch")
    rgb = np.ascontiguousarray(decoded[..., :3])
    alpha = (
        np.ascontiguousarray(decoded[..., 3])
        if mode == "RGBA"
        else np.full((height, width), 255, dtype=np.uint8)
    )
    rgb.setflags(write=False)
    alpha.setflags(write=False)
    return DecodedPng(
        rgb=rgb,
        alpha=alpha,
        mode=mode,
        sha256=_sha256_bytes(data),
        byte_count=len(data),
    )


def decoded_png_from_backend_snapshot(
    *,
    png_bytes: bytes,
    png_sha256: str,
    png_byte_count: int,
    decoder_mode: str,
    decoder_pixel_bytes: bytes,
    decoder_pixel_sha256: str,
) -> DecodedPng:
    """Construct a strict decode from one immutable backend snapshot.

    The backend has already run its decoder and attests the exact mode and pixel
    bytes.  This handoff independently validates the exact PNG container and
    all supplied hashes/bounds, but intentionally does not invoke Pillow again.
    Same-length pixel substitution is caught by ``decoder_pixel_sha256``.
    """

    if type(png_bytes) is not bytes or not 1 <= len(png_bytes) <= MAX_INPUT_BYTES:
        raise Spatial75Error("backend_snapshot_png_bytes_invalid")
    if type(decoder_pixel_bytes) is not bytes:
        raise Spatial75Error("backend_snapshot_pixel_bytes_not_immutable")
    if (
        type(png_byte_count) is not int
        or png_byte_count != len(png_bytes)
        or not _is_sha256(png_sha256)
        or _sha256_bytes(png_bytes) != png_sha256
    ):
        raise Spatial75Error("backend_snapshot_png_identity_mismatch")
    if (
        decoder_mode not in {"RGB", "RGBA"}
        or not _is_sha256(decoder_pixel_sha256)
        or _sha256_bytes(decoder_pixel_bytes) != decoder_pixel_sha256
    ):
        raise Spatial75Error("backend_snapshot_decoder_identity_mismatch")
    container_mode, _unused_idat = _validate_png_container(
        png_bytes,
        INPUT_SIZE,
        required_mode=decoder_mode,
        collect_idat=False,
    )
    if container_mode != decoder_mode:
        raise Spatial75Error("backend_snapshot_mode_mismatch")
    channels = 3 if decoder_mode == "RGB" else 4
    expected_pixel_bytes = INPUT_SIZE[0] * INPUT_SIZE[1] * channels
    if len(decoder_pixel_bytes) != expected_pixel_bytes:
        raise Spatial75Error("backend_snapshot_pixel_length_mismatch")

    decoded = np.frombuffer(decoder_pixel_bytes, dtype=np.uint8).reshape(
        INPUT_SIZE[1], INPUT_SIZE[0], channels
    )
    if decoder_mode == "RGB":
        rgb = decoded
        opaque_bytes = b"\xff" * (INPUT_SIZE[0] * INPUT_SIZE[1])
        alpha = np.frombuffer(opaque_bytes, dtype=np.uint8).reshape(
            INPUT_SIZE[1], INPUT_SIZE[0]
        )
    else:
        # Interleaved RGBA cannot expose RGB as a C-contiguous view. Copy each
        # retained plane once into immutable bytes; no image decoder is run.
        rgb_bytes = decoded[..., :3].tobytes(order="C")
        alpha_bytes = decoded[..., 3].tobytes(order="C")
        rgb = np.frombuffer(rgb_bytes, dtype=np.uint8).reshape(
            INPUT_SIZE[1], INPUT_SIZE[0], 3
        )
        alpha = np.frombuffer(alpha_bytes, dtype=np.uint8).reshape(
            INPUT_SIZE[1], INPUT_SIZE[0]
        )
    rgb.setflags(write=False)
    alpha.setflags(write=False)
    result = DecodedPng(
        rgb=rgb,
        alpha=alpha,
        mode=decoder_mode,
        sha256=png_sha256,
        byte_count=png_byte_count,
    )
    if (
        result.rgb.flags.writeable
        or result.alpha.flags.writeable
        or not result.rgb.flags.c_contiguous
        or not result.alpha.flags.c_contiguous
    ):
        raise Spatial75Error("backend_snapshot_plane_identity_mismatch")
    return result


def decode_png(path: str | Path) -> tuple[DecodedPng, dict[str, int]]:
    """Read and strictly decode one frozen-size policy input."""

    total_started = time.perf_counter_ns()
    data, read_ns = _read_bounded(path, MAX_INPUT_BYTES)
    decode_started = time.perf_counter_ns()
    decoded = _decode_png_bytes(data, INPUT_SIZE)
    decode_ns = time.perf_counter_ns() - decode_started
    return decoded, {
        "read": read_ns,
        "decode": decode_ns,
        "total": time.perf_counter_ns() - total_started,
    }


def _agreement_grid(size: tuple[int, int]) -> tuple[int, int]:
    width, height = size
    long_edge = max(width, height)
    long_tiles = min(
        AGREEMENT_MAX_LONG_EDGE_TILES,
        max(1, long_edge // AGREEMENT_MIN_TILE_EDGE),
    )

    def scaled(edge: int) -> int:
        return max(1, (long_tiles * edge + long_edge // 2) // long_edge)

    return scaled(width), scaled(height)


def _microtile_grid(size: tuple[int, int]) -> tuple[int, int]:
    width, height = size
    return max(1, width // MICROTILE_EDGE), max(1, height // MICROTILE_EDGE)


def _rectangles(
    size: tuple[int, int], columns: int, rows: int
) -> Iterable[tuple[int, int, int, int]]:
    width, height = size
    tile_count = columns * rows
    if not 1 <= tile_count <= MAX_AGREEMENT_TILES:
        raise Spatial75Error("agreement_grid_out_of_bounds")
    for row in range(rows):
        top = row * height // rows
        bottom = (row + 1) * height // rows
        for column in range(columns):
            left = column * width // columns
            right = (column + 1) * width // columns
            yield left, top, right, bottom


def _partition_channel_sums(
    difference: np.ndarray, columns: int, rows: int
) -> np.ndarray:
    """Return exact uint64 sums for one complete floor-partitioned grid.

    Equal-width partitions use a reshape/reduction.  Unequal floor partitions
    use ``add.reduceat`` at the exact backend boundaries.  Both routes scan
    every input pixel exactly once per grid and cannot overflow uint64 under the
    frozen dimensions and 8-bit channel bounds.
    """

    if (
        difference.shape != (INPUT_SIZE[1], INPUT_SIZE[0], 3)
        or difference.dtype != np.uint8
    ):
        raise Spatial75Error("difference_shape_mismatch")
    height, width, _channels = difference.shape
    if (
        not 1 <= columns <= width
        or not 1 <= rows <= height
        or columns * rows > MAX_AGREEMENT_TILES
    ):
        raise Spatial75Error("agreement_grid_out_of_bounds")
    if width * height * 255 > np.iinfo(np.uint64).max:
        raise Spatial75Error("agreement_integer_bound_exceeded")
    if height % rows == 0:
        row_groups = difference.reshape(
            rows, height // rows, width, 3
        ).sum(axis=1, dtype=np.uint64)
    else:
        row_starts = np.array(
            [row * height // rows for row in range(rows)], dtype=np.intp
        )
        row_groups = np.add.reduceat(
            difference, row_starts, axis=0, dtype=np.uint64
        )
    if width % columns == 0:
        result = row_groups.reshape(
            rows, columns, width // columns, 3
        ).sum(axis=2, dtype=np.uint64)
    else:
        column_starts = np.array(
            [column * width // columns for column in range(columns)],
            dtype=np.intp,
        )
        result = np.add.reduceat(
            row_groups, column_starts, axis=1, dtype=np.uint64
        )
    if result.shape != (rows, columns, 3) or result.dtype != np.uint64:
        raise Spatial75Error("partition_sum_identity_mismatch")
    result.setflags(write=False)
    return result


def _channel_sum_score(channel_sums: np.ndarray, pixels: int) -> float:
    if pixels <= 0:
        raise Spatial75Error("empty_agreement_rectangle")
    # Preserve ImageStat's operation order exactly: three independently divided
    # channel means, Python's left-to-right sum, then division by 3*255.
    if channel_sums.shape != (3,) or channel_sums.dtype != np.uint64:
        raise Spatial75Error("partition_channel_sum_identity_mismatch")
    means = [int(channel_sums[channel]) / pixels for channel in range(3)]
    value = 1.0 - sum(means) / (len(means) * 255.0)
    return min(1.0, max(0.0, float(value)))


def _agreement_from_decoded(
    draft: DecodedPng, verify: DecodedPng
) -> tuple[
    float,
    float,
    list[dict[str, Any]],
    float,
    int,
    dict[str, int],
]:
    started = time.perf_counter_ns()
    if draft.rgb.shape != verify.rgb.shape or draft.rgb.shape != (
        INPUT_SIZE[1],
        INPUT_SIZE[0],
        3,
    ):
        raise Spatial75Error("agreement_shape_mismatch")
    difference_started = time.perf_counter_ns()
    difference = np.abs(
        draft.rgb.astype(np.int16) - verify.rgb.astype(np.int16)
    ).astype(np.uint8)
    difference_ns = time.perf_counter_ns() - difference_started
    partition_started = time.perf_counter_ns()
    columns, rows = _agreement_grid(INPUT_SIZE)
    regional_sums = _partition_channel_sums(difference, columns, rows)
    micro_columns, micro_rows = _microtile_grid(INPUT_SIZE)
    micro_sums = _partition_channel_sums(
        difference, micro_columns, micro_rows
    )
    # The complete regional partition covers the full image exactly, so its
    # uint64 reduction is also the exact full-frame channel sum.
    global_sums = np.sum(regional_sums, axis=(0, 1), dtype=np.uint64)
    partition_ns = time.perf_counter_ns() - partition_started
    score_started = time.perf_counter_ns()
    global_score = _channel_sum_score(
        global_sums, INPUT_SIZE[0] * INPUT_SIZE[1]
    )
    tiles: list[dict[str, Any]] = []
    for index, rect in enumerate(_rectangles(INPUT_SIZE, columns, rows)):
        left, top, right, bottom = rect
        row, column = divmod(index, columns)
        tiles.append(
            {
                "rect": rect,
                "score": _channel_sum_score(
                    regional_sums[row, column],
                    (right - left) * (bottom - top),
                ),
            }
        )
    micro_scores = (
        _channel_sum_score(
            micro_sums[row, column],
            (right - left) * (bottom - top),
        )
        for index, (left, top, right, bottom) in enumerate(
            _rectangles(INPUT_SIZE, micro_columns, micro_rows)
        )
        for row, column in [divmod(index, micro_columns)]
    )
    microtile_worst = min(micro_scores)
    score_ns = time.perf_counter_ns() - score_started
    return (
        global_score,
        min(tile["score"] for tile in tiles),
        tiles,
        microtile_worst,
        micro_columns * micro_rows,
        {
            "difference": difference_ns,
            "partition": partition_ns,
            "score": score_ns,
            "total": time.perf_counter_ns() - started,
        },
    )


def _make_gate_result(
    *,
    draft: DecodedPng,
    verify: DecodedPng,
    global_score: float,
    regional: float,
    tiles: list[dict[str, Any]],
    micro: float,
    micro_count: int,
    timings: dict[str, int],
    draft_identity: str | None = None,
    verify_identity: str | None = None,
) -> GateResult:
    if draft_identity is None:
        draft_identity = _decoded_identity_sha256(draft)
    if verify_identity is None:
        verify_identity = _decoded_identity_sha256(verify)
    policy_sha = _policy_sha256()
    runtime_sha = _runtime_sha256()
    passed = (
        global_score >= GLOBAL_AGREEMENT_MIN
        and regional >= WORST_TILE_AGREEMENT_MIN
        and micro >= MICROTILE_AGREEMENT_MIN
    )
    result = GateResult(
        global_score=global_score,
        regional_worst=regional,
        tiles=tiles,
        microtile_worst=micro,
        microtile_count=micro_count,
        passed=passed,
        decoded_draft=draft,
        decoded_verify=verify,
        timings_ns=timings,
        draft_identity_sha256=draft_identity,
        verify_identity_sha256=verify_identity,
        policy_sha256=policy_sha,
        runtime_sha256=runtime_sha,
        binding_sha256="",
    )
    return replace(result, binding_sha256=_gate_binding_sha256(result))


def gate_pngs(draft_path: str | Path, verify_path: str | Path) -> GateResult:
    """Apply the frozen legacy L1 gate while retaining both decoded inputs."""

    total_started = time.perf_counter_ns()
    draft_data, draft_read = _read_bounded(draft_path, MAX_INPUT_BYTES)
    verify_data, verify_read = _read_bounded(verify_path, MAX_INPUT_BYTES)
    draft_decode_started = time.perf_counter_ns()
    draft = _decode_png_bytes(draft_data, INPUT_SIZE)
    draft_decode = time.perf_counter_ns() - draft_decode_started
    verify_decode_started = time.perf_counter_ns()
    verify = _decode_png_bytes(verify_data, INPUT_SIZE)
    verify_decode = time.perf_counter_ns() - verify_decode_started
    global_score, regional, tiles, micro, micro_count, metric_timings = (
        _agreement_from_decoded(draft, verify)
    )
    timings = {
        "read_draft": draft_read,
        "read_verify": verify_read,
        "decode_draft": draft_decode,
        "decode_verify": verify_decode,
        "difference": metric_timings["difference"],
        "partition": metric_timings["partition"],
        "score": metric_timings["score"],
        "total": 0,
    }
    result = _make_gate_result(
        draft=draft,
        verify=verify,
        global_score=global_score,
        regional=regional,
        tiles=tiles,
        micro=micro,
        micro_count=micro_count,
        timings=timings,
    )
    timings["total"] = time.perf_counter_ns() - total_started
    return result


def gate_decoded_pair(
    decoded_draft: DecodedPng, decoded_verify: DecodedPng
) -> GateResult:
    """Gate two retained strict decodes without path reads or image decodes."""

    total_started = time.perf_counter_ns()
    draft_identity = _decoded_identity_sha256(decoded_draft)
    verify_identity = _decoded_identity_sha256(decoded_verify)
    global_score, regional, tiles, micro, micro_count, metric_timings = (
        _agreement_from_decoded(decoded_draft, decoded_verify)
    )
    timings = {
        "read_draft": 0,
        "read_verify": 0,
        "decode_draft": 0,
        "decode_verify": 0,
        "difference": metric_timings["difference"],
        "partition": metric_timings["partition"],
        "score": metric_timings["score"],
        "total": 0,
    }
    result = _make_gate_result(
        draft=decoded_draft,
        verify=decoded_verify,
        global_score=global_score,
        regional=regional,
        tiles=tiles,
        micro=micro,
        micro_count=micro_count,
        timings=timings,
        draft_identity=draft_identity,
        verify_identity=verify_identity,
    )
    timings["total"] = time.perf_counter_ns() - total_started
    return result


def gate_decoded_draft(
    decoded_draft: DecodedPng, verify_path: str | Path
) -> GateResult:
    """Gate a retained draft decode against one newly available verify PNG."""

    total_started = time.perf_counter_ns()
    # Validate before doing I/O so a changed or mutable draft fails closed.
    draft_identity = _decoded_identity_sha256(decoded_draft)
    verify_data, verify_read = _read_bounded(verify_path, MAX_INPUT_BYTES)
    verify_decode_started = time.perf_counter_ns()
    verify = _decode_png_bytes(verify_data, INPUT_SIZE)
    verify_decode = time.perf_counter_ns() - verify_decode_started
    global_score, regional, tiles, micro, micro_count, metric_timings = (
        _agreement_from_decoded(decoded_draft, verify)
    )
    timings = {
        "read_draft": 0,
        "read_verify": verify_read,
        "decode_draft": 0,
        "decode_verify": verify_decode,
        "difference": metric_timings["difference"],
        "partition": metric_timings["partition"],
        "score": metric_timings["score"],
        "total": 0,
    }
    result = _make_gate_result(
        draft=decoded_draft,
        verify=verify,
        global_score=global_score,
        regional=regional,
        tiles=tiles,
        micro=micro,
        micro_count=micro_count,
        timings=timings,
        draft_identity=draft_identity,
    )
    timings["total"] = time.perf_counter_ns() - total_started
    return result


def _encode_transformed(decoded: DecodedPng) -> tuple[bytes, int, int]:
    expected_rgb_shape = (INPUT_SIZE[1], INPUT_SIZE[0], 3)
    expected_alpha_shape = (INPUT_SIZE[1], INPUT_SIZE[0])
    if (
        decoded.rgb.dtype != np.uint8
        or decoded.alpha.dtype != np.uint8
        or decoded.rgb.shape != expected_rgb_shape
        or decoded.alpha.shape != expected_alpha_shape
    ):
        raise Spatial75Error("decoded_input_identity_mismatch")
    _assert_operator_pins()
    transform_started = time.perf_counter_ns()
    rgb = Image.fromarray(decoded.rgb, "RGB").resize(
        OUTPUT_SIZE, resample=Image.Resampling.BICUBIC
    )
    alpha = Image.fromarray(decoded.alpha, "L").resize(
        OUTPUT_SIZE, resample=Image.Resampling.BICUBIC
    )
    red, green, blue = rgb.split()
    output = Image.merge("RGBA", (red, green, blue, alpha))
    transform_ns = time.perf_counter_ns() - transform_started
    encode_started = time.perf_counter_ns()
    encoded = BytesIO()
    output.save(
        encoded,
        format="PNG",
        optimize=PNG_OPTIMIZE,
        compress_level=PNG_COMPRESSION_LEVEL,
    )
    data = encoded.getvalue()
    encode_ns = time.perf_counter_ns() - encode_started
    if not 1 <= len(data) <= MAX_OUTPUT_BYTES:
        raise Spatial75Error("encoded_png_size_out_of_bounds")
    return data, transform_ns, encode_ns


def _validate_encoded_output(data: bytes) -> None:
    mode, compressed = _validate_png_container(
        data, OUTPUT_SIZE, required_mode="RGBA"
    )
    if mode != "RGBA" or len(compressed) < 3:
        raise Spatial75Error("encoded_png_identity_mismatch")
    # A zlib stream records only a coarse compression class.  Level zero as
    # emitted by the pinned Pillow operator is FLEVEL=0 and begins with a stored
    # (uncompressed) DEFLATE block; validate both observable properties.
    cmf, flg, first_deflate = compressed[:3]
    if (
        cmf != 0x78
        or (cmf * 256 + flg) % 31 != 0
        or flg & 0x20
        or flg >> 6 != 0
        or ((first_deflate >> 1) & 0b11) != 0
    ):
        raise Spatial75Error("png_compression_level_not_zero")
    _pillow_decode_validated_png(
        data, OUTPUT_SIZE, mode, capture_pixels=False
    )


def prepare_decoded_draft(decoded_draft: DecodedPng) -> PreparedOutput:
    """Prepare and fully validate immutable output bytes without publishing."""

    total_started = time.perf_counter_ns()
    identity_started = time.perf_counter_ns()
    draft_identity = _decoded_identity_sha256(decoded_draft)
    policy_sha = _policy_sha256()
    runtime_sha = _runtime_sha256()
    identity_ns = time.perf_counter_ns() - identity_started
    data, transform_ns, encode_ns = _encode_transformed(decoded_draft)
    validate_started = time.perf_counter_ns()
    _validate_encoded_output(data)
    validate_ns = time.perf_counter_ns() - validate_started
    bind_started = time.perf_counter_ns()
    output_sha = _sha256_bytes(data)
    provisional_timings = PreparedTimings(
        identity=identity_ns,
        transform=transform_ns,
        encode=encode_ns,
        validate=validate_ns,
        bind=0,
        total=0,
    )
    result = PreparedOutput(
        encoded_png=data,
        input_sha256=decoded_draft.sha256,
        input_decoded_identity_sha256=draft_identity,
        input_mode=decoded_draft.mode,
        input_bytes=decoded_draft.byte_count,
        output_sha256=output_sha,
        output_bytes=len(data),
        policy_sha256=policy_sha,
        runtime_sha256=runtime_sha,
        binding_sha256="",
        timings_ns=provisional_timings,
        _seal=None,
    )
    binding = _prepared_binding_sha256(result)
    bind_ns = time.perf_counter_ns() - bind_started
    timings = PreparedTimings(
        identity=identity_ns,
        transform=transform_ns,
        encode=encode_ns,
        validate=validate_ns,
        bind=bind_ns,
        total=time.perf_counter_ns() - total_started,
    )
    final = replace(result, binding_sha256=binding, timings_ns=timings)
    seal = _PreparedSeal(
        encoded_png=final.encoded_png,
        input_sha256=final.input_sha256,
        input_decoded_identity_sha256=final.input_decoded_identity_sha256,
        input_mode=final.input_mode,
        input_bytes=final.input_bytes,
        output_sha256=final.output_sha256,
        output_bytes=final.output_bytes,
        policy_sha256=final.policy_sha256,
        runtime_sha256=final.runtime_sha256,
        binding_sha256=final.binding_sha256,
        timings_ns=final.timings_ns,
    )
    return replace(final, _seal=seal)


def _publish_new(path: Path, data: bytes) -> int:
    """Create ``path`` without replacement and verify the exposed inode/bytes.

    Same-UID writers remain inside the storage trust boundary: as with any
    pathname-based POSIX publication, they must not mutate the directory during
    the transaction or after return.  Retaining both descriptors through the
    parent-directory fsync closes accidental and pre-return stage substitution.
    """

    started = time.perf_counter_ns()
    if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_OUTPUT_BYTES:
        raise Spatial75Error("invalid_output_bytes")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise Spatial75Error("unsafe_output_parent")
    if path.exists() or path.is_symlink():
        raise Spatial75Error("output_exists")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(16)}.tmp")
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    stage_fd = os.open(temporary, flags, 0o600)
    destination_fd: int | None = None
    destination_linked = False
    publication_complete = False
    try:
        view = memoryview(data)
        while view:
            written = os.write(stage_fd, view)
            if written <= 0:
                raise Spatial75Error("publication_short_write")
            view = view[written:]
        os.fsync(stage_fd)
        staged = os.fstat(stage_fd)
        if not stat.S_ISREG(staged.st_mode) or staged.st_size != len(data):
            raise Spatial75Error("publication_identity")
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise Spatial75Error("output_exists") from exc
        destination_linked = True

        destination_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        destination_flags |= getattr(os, "O_NOFOLLOW", 0)
        destination_fd = os.open(path, destination_flags)
        exposed = os.fstat(destination_fd)
        staged_after_link = os.fstat(stage_fd)
        if (
            exposed.st_dev != staged_after_link.st_dev
            or exposed.st_ino != staged_after_link.st_ino
            or exposed.st_size != len(data)
            or not stat.S_ISREG(exposed.st_mode)
        ):
            raise Spatial75Error("publication_identity")

        temporary.unlink()
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

        final_stage = os.fstat(stage_fd)
        final_exposed = os.fstat(destination_fd)
        current = path.lstat()
        if (
            final_stage.st_dev != final_exposed.st_dev
            or final_stage.st_ino != final_exposed.st_ino
            or current.st_dev != final_exposed.st_dev
            or current.st_ino != final_exposed.st_ino
            or current.st_size != len(data)
            or not stat.S_ISREG(current.st_mode)
        ):
            raise Spatial75Error("publication_identity")

        os.fsync(destination_fd)
        publication_complete = True
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        os.close(stage_fd)
        temporary.unlink(missing_ok=True)
        if destination_linked and not publication_complete:
            path.unlink(missing_ok=True)
            try:
                directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                directory_fd = os.open(path.parent, directory_flags)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
    return time.perf_counter_ns() - started


def publish_prepared_after_gate(
    prepared: PreparedOutput,
    gate: GateResult,
    output_path: str | Path,
) -> PostprocessResult:
    """Atomically publish immutable prepared bytes only after a bound pass."""

    total_started = time.perf_counter_ns()
    _validate_gate_result(gate)
    if not gate.passed:
        raise GateRejected()
    revalidate_started = time.perf_counter_ns()
    _validate_prepared_output(prepared)
    if (
        prepared.input_sha256 != gate.decoded_draft.sha256
        or prepared.input_decoded_identity_sha256
        != gate.draft_identity_sha256
        or prepared.input_mode != gate.decoded_draft.mode
        or prepared.input_bytes != gate.decoded_draft.byte_count
        or prepared.policy_sha256 != gate.policy_sha256
        or prepared.runtime_sha256 != gate.runtime_sha256
    ):
        raise Spatial75Error("prepared_gate_draft_identity_mismatch")
    revalidate_ns = time.perf_counter_ns() - revalidate_started
    publish_ns = _publish_new(Path(output_path), prepared.encoded_png)
    call_ns = time.perf_counter_ns() - total_started
    timings = {
        "read": 0,
        "decode": 0,
        "transform": prepared.timings_ns.transform,
        "encode": prepared.timings_ns.encode,
        "validate": prepared.timings_ns.validate + revalidate_ns,
        "publish": publish_ns,
        # Total CPU-stage work is useful when preparation overlaps rendering;
        # publication_call records the short post-gate wall-clock tail.
        "total": prepared.timings_ns.total + revalidate_ns + publish_ns,
        "prepare_identity": prepared.timings_ns.identity,
        "prepare_bind": prepared.timings_ns.bind,
        "prepared_total": prepared.timings_ns.total,
        "publication_call": call_ns,
    }
    return PostprocessResult(
        output_path=Path(output_path).resolve(),
        input_sha256=prepared.input_sha256,
        output_sha256=prepared.output_sha256,
        output_bytes=prepared.output_bytes,
        timings_ns=timings,
        input_decode_reused=True,
    )


def gate_decoded_pair_and_publish_prepared(
    prepared: PreparedOutput,
    decoded_draft: DecodedPng,
    decoded_verify: DecodedPng,
    output_path: str | Path,
) -> FusedGatePublishResult:
    """Gate two retained decodes and atomically publish in one authorization.

    The prepared seal binds the exact immutable byte object created by
    :func:`prepare_decoded_draft`, so this fused path does not re-hash that
    8.3-MiB PNG.  Each decoded input is hashed exactly once, before scoring; the
    locally constructed gate is not round-tripped through the public gate
    validator.  Rejection exits before the publication primitive is called.
    """

    total_started = time.perf_counter_ns()
    authorization_started = time.perf_counter_ns()
    _validate_prepared_seal(prepared)
    draft_identity = _decoded_identity_sha256(decoded_draft)
    verify_identity = _decoded_identity_sha256(decoded_verify)
    if (
        prepared.input_sha256 != decoded_draft.sha256
        or prepared.input_decoded_identity_sha256 != draft_identity
        or prepared.input_mode != decoded_draft.mode
        or prepared.input_bytes != decoded_draft.byte_count
    ):
        raise Spatial75Error("prepared_gate_draft_identity_mismatch")
    authorization_ns = time.perf_counter_ns() - authorization_started

    global_score, regional, tiles, micro, micro_count, metric_timings = (
        _agreement_from_decoded(decoded_draft, decoded_verify)
    )
    gate_timings = {
        "read_draft": 0,
        "read_verify": 0,
        "decode_draft": 0,
        "decode_verify": 0,
        "difference": metric_timings["difference"],
        "partition": metric_timings["partition"],
        "score": metric_timings["score"],
        "authorization": authorization_ns,
        "total": 0,
    }
    gate = _make_gate_result(
        draft=decoded_draft,
        verify=decoded_verify,
        global_score=global_score,
        regional=regional,
        tiles=tiles,
        micro=micro,
        micro_count=micro_count,
        timings=gate_timings,
        draft_identity=draft_identity,
        verify_identity=verify_identity,
    )
    gate_timings["total"] = time.perf_counter_ns() - total_started
    if (
        gate.policy_sha256 != prepared.policy_sha256
        or gate.runtime_sha256 != prepared.runtime_sha256
    ):
        raise Spatial75Error("prepared_gate_runtime_identity_mismatch")
    if not gate.passed:
        raise GateRejected()

    publish_ns = _publish_new(Path(output_path), prepared.encoded_png)
    fused_ns = time.perf_counter_ns() - total_started
    postprocess_timings = {
        "read": 0,
        "decode": 0,
        "transform": prepared.timings_ns.transform,
        "encode": prepared.timings_ns.encode,
        "validate": prepared.timings_ns.validate + authorization_ns,
        "publish": publish_ns,
        "total": prepared.timings_ns.total + authorization_ns + publish_ns,
        "prepare_identity": prepared.timings_ns.identity,
        "prepare_bind": prepared.timings_ns.bind,
        "prepared_total": prepared.timings_ns.total,
        "fused_authorization_call": fused_ns,
    }
    postprocess = PostprocessResult(
        output_path=Path(output_path).resolve(),
        input_sha256=prepared.input_sha256,
        output_sha256=prepared.output_sha256,
        output_bytes=prepared.output_bytes,
        timings_ns=postprocess_timings,
        input_decode_reused=True,
    )
    return FusedGatePublishResult(
        gate=gate,
        postprocess=postprocess,
        timings_ns={
            "authorization": authorization_ns,
            "difference": metric_timings["difference"],
            "partition": metric_timings["partition"],
            "score": metric_timings["score"],
            "publish": publish_ns,
            "total": fused_ns,
        },
    )


def postprocess_decoded(
    decoded: DecodedPng,
    output_path: str | Path,
    *,
    input_decode_reused: bool = True,
) -> PostprocessResult:
    """Resize a retained strict decode and atomically publish one RGBA8 PNG."""

    total_started = time.perf_counter_ns()
    output = Path(output_path)
    if output.exists() or output.is_symlink():
        raise Spatial75Error("output_exists")
    data, transform_ns, encode_ns = _encode_transformed(decoded)
    validate_started = time.perf_counter_ns()
    _validate_encoded_output(data)
    validate_ns = time.perf_counter_ns() - validate_started
    publish_ns = _publish_new(output, data)
    timings = {
        "read": 0,
        "decode": 0,
        "transform": transform_ns,
        "encode": encode_ns,
        "validate": validate_ns,
        "publish": publish_ns,
        "total": time.perf_counter_ns() - total_started,
    }
    return PostprocessResult(
        output_path=output.resolve(),
        input_sha256=decoded.sha256,
        output_sha256=_sha256_bytes(data),
        output_bytes=len(data),
        timings_ns=timings,
        input_decode_reused=input_decode_reused,
    )


def postprocess_png(
    input_path: str | Path, output_path: str | Path
) -> PostprocessResult:
    """Read, decode, resize, validate, and publish one strict input PNG."""

    total_started = time.perf_counter_ns()
    decoded, decode_timings = decode_png(input_path)
    result = postprocess_decoded(
        decoded, output_path, input_decode_reused=False
    )
    timings = dict(result.timings_ns)
    timings["read"] = decode_timings["read"]
    timings["decode"] = decode_timings["decode"]
    timings["total"] = time.perf_counter_ns() - total_started
    return replace(result, timings_ns=timings)


def benchmark_gate_and_postprocess(
    draft_path: str | Path,
    verify_path: str | Path,
    output_path: str | Path,
) -> BenchmarkResult:
    """Time the gate plus selected-draft postprocess as one product path.

    A rejected draft is not selected and no output is written.  A passing draft
    reuses the gate's strict decode; the postprocess therefore records zero
    additional input decode time.
    """

    total_started = time.perf_counter_ns()
    gate = gate_pngs(draft_path, verify_path)
    if not gate.passed:
        raise GateRejected()
    postprocess = postprocess_decoded(
        gate.decoded_draft, output_path, input_decode_reused=True
    )
    total = time.perf_counter_ns() - total_started
    timings = {
        "read": gate.timings_ns["read_draft"]
        + gate.timings_ns["read_verify"],
        "decode": gate.timings_ns["decode_draft"]
        + gate.timings_ns["decode_verify"],
        "gate_difference": gate.timings_ns["difference"],
        "gate_partition": gate.timings_ns["partition"],
        "gate_score": gate.timings_ns["score"],
        "transform": postprocess.timings_ns["transform"],
        "encode": postprocess.timings_ns["encode"],
        "validate": postprocess.timings_ns["validate"],
        "publish": postprocess.timings_ns["publish"],
        "gate": gate.timings_ns["total"],
        "selected_draft_postprocess": postprocess.timings_ns["total"],
        "total": total,
    }
    return BenchmarkResult(gate=gate, postprocess=postprocess, timings_ns=timings)


def _write_receipt_new(path: str | Path, value: dict[str, Any]) -> None:
    encoded = json.dumps(
        value, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False
    ).encode("ascii") + b"\n"
    _publish_new(Path(path), encoded)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    post = subparsers.add_parser("postprocess")
    post.add_argument("input", type=Path)
    post.add_argument("output", type=Path)
    post.add_argument("--receipt", type=Path)
    benchmark = subparsers.add_parser("benchmark")
    benchmark.add_argument("draft", type=Path)
    benchmark.add_argument("verify", type=Path)
    benchmark.add_argument("output", type=Path)
    benchmark.add_argument("--receipt", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "postprocess":
            result: PostprocessResult | BenchmarkResult = postprocess_png(
                args.input, args.output
            )
        else:
            result = benchmark_gate_and_postprocess(
                args.draft, args.verify, args.output
            )
        receipt = result.receipt()
        if args.receipt is not None:
            _write_receipt_new(args.receipt, receipt)
        print(json.dumps(receipt, sort_keys=True, allow_nan=False))
        return 0
    except Spatial75Error as exc:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": RESULT_KIND,
                    "pass": False,
                    "error": exc.code,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
