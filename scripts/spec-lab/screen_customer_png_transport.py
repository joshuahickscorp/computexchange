#!/usr/bin/env python3
"""Measure lossless PNG encoding versus modeled customer wire time.

The measured component is in-memory PNG encoding of one already-decoded image.
Wire time is arithmetic from encoded bytes and a user-supplied decimal Mbps line
rate.  RTT, TLS, queueing, congestion, HTTP framing, browser decode, and display
are not measured.  For a single-frame RGB/RGBA input, re-encoding preserves raw
decoded RGBA code values exactly but changes the encoded-byte, color-metadata,
and ancillary-metadata contracts, so this screen cannot be cited as an exact-
cache hit or color-managed display equivalence.
"""

from __future__ import annotations

import argparse
from io import BytesIO
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import secrets
import stat
import statistics
import sys
import time
from typing import Any, Iterable, Sequence
import zlib

import PIL
from PIL import Image


SCHEMA_VERSION = 1
KIND = "cx_customer_png_transport_screen"
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_DIMENSION = 16_384
MAX_PIXELS = 64 * 1024 * 1024
DEFAULT_LEVELS = (0, 1, 3, 6, 9)
DEFAULT_LINK_MBPS = (25.0, 50.0, 100.0, 500.0, 1000.0)
MAX_LINK_RATES = 64
MAX_RECEIPT_BYTES = 4 * 1024 * 1024
MAX_CODE_BYTES = 4 * 1024 * 1024
MAX_PATH_BYTES = 4_096
MAX_ENCODED_BYTES = MAX_SOURCE_BYTES + 4 * MAX_PIXELS + (1 << 20)

CLAIM_SCOPE = (
    "local single-frame PNG encode measurement with exact raw decoded-RGBA code "
    "values plus arithmetic line-rate wire floor; not exact encoded-byte "
    "transport, color-managed display equivalence, metadata equivalence, or "
    "end-to-end customer latency"
)
EVIDENCE = "measured_local_unattested"
EXCLUDED = (
    "source_decode_and_render",
    "queue_and_cold_start",
    "RTT_TLS_HTTP_and_congestion",
    "browser_decode_and_display",
    "ICC_gamma_and_ancillary_metadata_equivalence",
    "durable_or_network_publication",
)

_TOP_LEVEL_KEYS = {
    "claim_scope",
    "comparison_baseline_level",
    "environment",
    "evidence",
    "excluded",
    "kind",
    "pareto_compression_levels",
    "pins",
    "receipt_sha256",
    "rows",
    "schema_version",
    "source",
    "timing",
}
_SOURCE_KEYS = {
    "bytes",
    "decoded_mode",
    "decoded_rgba_bytes",
    "decoded_rgba_sha256",
    "frame_count",
    "height",
    "path",
    "sha256",
    "source_device",
    "source_inode",
    "width",
}
_TIMING_KEYS = {
    "clock",
    "order",
    "trial_count_per_level",
    "warmup_per_level",
}
_ROW_KEYS = {
    "compress_level",
    "decoded_rgba_exact",
    "decoded_rgba_sha256",
    "encode_median_s",
    "encode_median_speedup_vs_baseline_x",
    "encode_p95_s_type7",
    "encode_samples_s",
    "encoded_bytes",
    "encoded_deterministic_across_trials",
    "encoded_sha256",
    "encoded_size_reduction_vs_baseline_x",
    "encoded_trial_identities",
    "link_models",
}
_TRIAL_IDENTITY_KEYS = {"bytes", "sha256"}
_LINK_MODEL_KEYS = {
    "encode_plus_wire_s",
    "encode_plus_wire_speedup_vs_baseline_x",
    "link_mbps_decimal",
    "wire_floor_s",
}
_PIN_KEYS = {"screen_module", "screen_test"}
_PIN_RECORD_KEYS = {"bytes", "path", "sha256"}
_ENVIRONMENT_KEYS = {
    "byteorder",
    "cpu_count",
    "machine",
    "perf_counter_adjustable",
    "perf_counter_implementation",
    "perf_counter_monotonic",
    "perf_counter_resolution_s",
    "pillow_version",
    "platform_release",
    "platform_system",
    "python_implementation",
    "python_version",
    "zlib_compile_version",
    "zlib_runtime_version",
}


class PngTransportError(RuntimeError):
    pass


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _identity(info: os.stat_result) -> tuple[int, ...]:
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


def _read_bounded_regular_file(path: Path, maximum: int, label: str) -> bytes:
    """Read one immutable-looking, singly-linked file snapshot by descriptor."""

    if type(maximum) is not int or maximum <= 0:
        raise PngTransportError("internal file bound is invalid")
    try:
        path_before = path.lstat()
    except OSError as exc:
        raise PngTransportError(f"{label} is unavailable") from exc
    if not stat.S_ISREG(path_before.st_mode) or stat.S_ISLNK(path_before.st_mode):
        raise PngTransportError(f"{label} must be a regular non-symlink file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PngTransportError(f"{label} failed no-follow open") from exc
    data = bytearray()
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or _identity(before) != _identity(path_before)
        ):
            raise PngTransportError(f"{label} identity changed before read")
        if before.st_size <= 0 or before.st_size > maximum:
            raise PngTransportError(f"{label} size is outside 1..{maximum} bytes")
        while len(data) <= maximum:
            chunk = os.read(
                descriptor,
                min(1 << 20, maximum + 1 - len(data)),
            )
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        if _identity(before) != _identity(after) or len(data) != after.st_size:
            raise PngTransportError(f"{label} changed while read")
    finally:
        os.close(descriptor)
    if len(data) > maximum:
        raise PngTransportError(f"{label} exceeds {maximum} bytes")
    try:
        path_after = path.lstat()
    except OSError as exc:
        raise PngTransportError(f"{label} disappeared after read") from exc
    if _identity(path_after) != _identity(after) or stat.S_ISLNK(path_after.st_mode):
        raise PngTransportError(f"{label} path changed after read")
    return bytes(data)


def sha256_file(path: Path) -> str:
    return sha256_bytes(_read_bounded_regular_file(path, MAX_CODE_BYTES, "code pin"))


def canonical_json(value: Any, *, pretty: bool = False) -> bytes:
    kwargs: dict[str, Any] = {
        "allow_nan": False,
        "sort_keys": True,
    }
    if pretty:
        kwargs["indent"] = 2
    else:
        kwargs["separators"] = (",", ":")
    return (json.dumps(value, **kwargs) + "\n").encode()


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PngTransportError(f"duplicate receipt JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise PngTransportError(f"invalid receipt JSON constant {value}")


def parse_receipt(raw: bytes) -> dict[str, Any]:
    """Parse bounded duplicate-free finite UTF-8 receipt JSON."""

    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_RECEIPT_BYTES:
        raise PngTransportError(
            f"receipt must contain 1..{MAX_RECEIPT_BYTES} bytes"
        )
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_pairs,
            parse_constant=_reject_constant,
        )
    except PngTransportError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise PngTransportError("receipt is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise PngTransportError("receipt root must be an object")
    return value


def _exact_dict(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise PngTransportError(f"{label} has an unknown or missing field")
    return value


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise PngTransportError(
            f"{label} must be an integer in [{minimum},{maximum}]"
        )
    return value


def _finite(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PngTransportError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0.0):
        suffix = " positive" if positive else " finite"
        raise PngTransportError(f"{label} must be{suffix}")
    return result


def _sha256(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise PngTransportError(f"{label} must be lowercase SHA-256")
    return value


def _bounded_text(value: Any, label: str, maximum: int = MAX_PATH_BYTES) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise PngTransportError(f"{label} must be a nonempty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PngTransportError(f"{label} must be valid UTF-8") from exc
    if len(encoded) > maximum:
        raise PngTransportError(f"{label} exceeds {maximum} UTF-8 bytes")
    return value


def _file_pin(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    payload = _read_bounded_regular_file(resolved, MAX_CODE_BYTES, "code pin")
    return {
        "bytes": len(payload),
        "path": str(resolved),
        "sha256": sha256_bytes(payload),
    }


def current_code_pins() -> dict[str, dict[str, Any]]:
    module_path = Path(__file__).resolve(strict=True)
    test_path = module_path.with_name(f"test_{module_path.name}")
    if not test_path.is_file() or test_path.is_symlink():
        raise PngTransportError("closed screen requires its regular test module")
    return {
        "screen_module": _file_pin(module_path),
        "screen_test": _file_pin(test_path),
    }


def current_environment() -> dict[str, Any]:
    clock = time.get_clock_info("perf_counter")
    return {
        "byteorder": sys.byteorder,
        "cpu_count": os.cpu_count() or 0,
        "machine": platform.machine(),
        "perf_counter_adjustable": clock.adjustable,
        "perf_counter_implementation": clock.implementation,
        "perf_counter_monotonic": clock.monotonic,
        "perf_counter_resolution_s": clock.resolution,
        "pillow_version": PIL.__version__,
        "platform_release": platform.release(),
        "platform_system": platform.system(),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "zlib_compile_version": zlib.ZLIB_VERSION,
        "zlib_runtime_version": zlib.ZLIB_RUNTIME_VERSION,
    }


def receipt_sha256(receipt: dict[str, Any]) -> str:
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256", None)
    return sha256_bytes(canonical_json(unsigned))


def type7(values: Sequence[float], probability: float) -> float:
    if not values:
        raise PngTransportError("quantile requires samples")
    if not 0.0 <= probability <= 1.0:
        raise PngTransportError("quantile probability is outside [0,1]")
    ordered = sorted(float(value) for value in values)
    if any(not math.isfinite(value) for value in ordered):
        raise PngTransportError("quantile samples must be finite")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def validate_levels(values: Iterable[int]) -> list[int]:
    result: list[int] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 9:
            raise PngTransportError("PNG compression levels must be integers in [0,9]")
        result.append(value)
    if not result or len(result) != len(set(result)):
        raise PngTransportError("PNG compression levels must be nonempty and unique")
    return sorted(result)


def validate_link_rates(values: Iterable[float]) -> list[float]:
    result: list[float] = []
    for raw in values:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise PngTransportError("link rates must be numeric")
        value = float(raw)
        if not math.isfinite(value) or value <= 0.0 or value > 1_000_000.0:
            raise PngTransportError("link rates must be finite and in (0,1000000]")
        result.append(value)
    if (
        not result
        or len(result) > MAX_LINK_RATES
        or len(result) != len(set(result))
    ):
        raise PngTransportError("link rates must be nonempty and unique")
    return sorted(result)


def load_png(path: Path) -> tuple[Image.Image, dict[str, Any]]:
    try:
        path_info = path.lstat()
    except OSError as exc:
        raise PngTransportError("source PNG is unavailable") from exc
    if stat.S_ISLNK(path_info.st_mode) or not stat.S_ISREG(path_info.st_mode):
        raise PngTransportError("source must be a regular non-symlink file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PngTransportError("source PNG failed no-follow open") from exc
    source = bytearray()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise PngTransportError("source must be a singly-linked regular file")
        if before.st_size <= 0 or before.st_size > MAX_SOURCE_BYTES:
            raise PngTransportError("source PNG size is outside the closed bound")
        while len(source) <= MAX_SOURCE_BYTES:
            chunk = os.read(
                descriptor,
                min(1 << 20, MAX_SOURCE_BYTES + 1 - len(source)),
            )
            if not chunk:
                break
            source.extend(chunk)
        after = os.fstat(descriptor)
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
        if identity_before != identity_after or len(source) != after.st_size:
            raise PngTransportError("source PNG changed while reading")
    finally:
        os.close(descriptor)
    if len(source) > MAX_SOURCE_BYTES:
        raise PngTransportError("source PNG exceeds the closed byte bound")
    source_bytes = bytes(source)
    try:
        with Image.open(BytesIO(source_bytes)) as opened:
            if opened.format != "PNG" or opened.mode not in {"RGB", "RGBA"}:
                raise PngTransportError("source must be an RGB/RGBA PNG")
            if getattr(opened, "n_frames", 1) != 1 or getattr(
                opened, "is_animated", False
            ):
                raise PngTransportError("animated PNG is outside the single-frame contract")
            width, height = opened.size
            if (
                width <= 0
                or height <= 0
                or width > MAX_DIMENSION
                or height > MAX_DIMENSION
                or width * height > MAX_PIXELS
            ):
                raise PngTransportError("decoded dimensions exceed the closed bound")
            opened.load()
            image = opened.convert("RGBA")
    except PngTransportError:
        raise
    except Exception as exc:
        raise PngTransportError("source PNG failed strict decode") from exc
    raw = image.tobytes()
    return image, {
        "bytes": len(source_bytes),
        "decoded_mode": "RGBA",
        "decoded_rgba_bytes": len(raw),
        "decoded_rgba_sha256": sha256_bytes(raw),
        "frame_count": 1,
        "height": height,
        "path": str(path.absolute()),
        "sha256": sha256_bytes(source_bytes),
        "source_device": before.st_dev,
        "source_inode": before.st_ino,
        "width": width,
    }


def encode_png(image: Image.Image, level: int) -> tuple[bytes, float]:
    output = BytesIO()
    started = time.perf_counter_ns()
    image.save(
        output,
        format="PNG",
        compress_level=level,
        optimize=False,
    )
    elapsed_s = (time.perf_counter_ns() - started) / 1_000_000_000.0
    return output.getvalue(), elapsed_s


def decoded_rgba_sha256(payload: bytes) -> str:
    try:
        with Image.open(BytesIO(payload)) as image:
            if image.format != "PNG" or image.mode not in {"RGB", "RGBA"}:
                raise PngTransportError("encoded candidate is not RGB/RGBA PNG")
            if getattr(image, "n_frames", 1) != 1 or getattr(
                image, "is_animated", False
            ):
                raise PngTransportError("encoded candidate must be single-frame PNG")
            image.load()
            return sha256_bytes(image.convert("RGBA").tobytes())
    except PngTransportError:
        raise
    except Exception as exc:
        raise PngTransportError("encoded candidate failed decode") from exc


def pareto_levels(rows: Sequence[dict[str, Any]]) -> list[int]:
    result: list[int] = []
    for row in rows:
        dominated = any(
            other["encoded_bytes"] <= row["encoded_bytes"]
            and other["encode_median_s"] <= row["encode_median_s"]
            and (
                other["encoded_bytes"] < row["encoded_bytes"]
                or other["encode_median_s"] < row["encode_median_s"]
            )
            for other in rows
        )
        if not dominated:
            result.append(row["compress_level"])
    return sorted(result)


def run_screen(
    source: Path,
    *,
    levels: Iterable[int] = DEFAULT_LEVELS,
    trials: int = 7,
    link_rates_mbps: Iterable[float] = DEFAULT_LINK_MBPS,
) -> dict[str, Any]:
    starting_pins = current_code_pins()
    environment = current_environment()
    levels_list = validate_levels(levels)
    link_rates = validate_link_rates(link_rates_mbps)
    if isinstance(trials, bool) or not isinstance(trials, int):
        raise PngTransportError("trials must be an integer")
    if trials < 3 or trials > 31 or trials % 2 == 0:
        raise PngTransportError("trials must be odd and in [3,31]")
    image, source_record = load_png(source)
    expected_raw_sha = source_record["decoded_rgba_sha256"]

    # Warm every codec setting once. Rotate the measured order to reduce a
    # stable level/thermal position bias without randomizing the experiment.
    for level in levels_list:
        warm_payload, _ = encode_png(image, level)
        if decoded_rgba_sha256(warm_payload) != expected_raw_sha:
            raise PngTransportError("warmup lossless decoded-RGBA check failed")
    samples: dict[int, list[float]] = {level: [] for level in levels_list}
    payloads: dict[int, bytes] = {}
    payload_identities: dict[int, list[tuple[int, str]]] = {
        level: [] for level in levels_list
    }
    for trial in range(trials):
        offset = trial % len(levels_list)
        order = levels_list[offset:] + levels_list[:offset]
        for level in order:
            payload, elapsed_s = encode_png(image, level)
            if decoded_rgba_sha256(payload) != expected_raw_sha:
                raise PngTransportError(
                    "measured lossless decoded-RGBA identity check failed"
                )
            samples[level].append(elapsed_s)
            payloads[level] = payload
            payload_identities[level].append((len(payload), sha256_bytes(payload)))

    rows: list[dict[str, Any]] = []
    for level in levels_list:
        payload = payloads[level]
        candidate_raw_sha = decoded_rgba_sha256(payload)
        if candidate_raw_sha != expected_raw_sha:
            raise PngTransportError("lossless decoded-RGBA identity check failed")
        if len(set(payload_identities[level])) != 1:
            raise PngTransportError("PNG encoder output changed across measured trials")
        encode_samples = samples[level]
        median_s = statistics.median(encode_samples)
        row = {
            "compress_level": level,
            "decoded_rgba_exact": True,
            "decoded_rgba_sha256": candidate_raw_sha,
            "encode_median_s": median_s,
            "encode_p95_s_type7": type7(encode_samples, 0.95),
            "encode_samples_s": encode_samples,
            "encoded_bytes": len(payload),
            "encoded_deterministic_across_trials": True,
            "encoded_sha256": sha256_bytes(payload),
            "encoded_trial_identities": [
                {"bytes": byte_count, "sha256": digest}
                for byte_count, digest in payload_identities[level]
            ],
            "link_models": [
                {
                    "encode_plus_wire_s": median_s
                    + len(payload) * 8.0 / (rate * 1_000_000.0),
                    "link_mbps_decimal": rate,
                    "wire_floor_s": len(payload) * 8.0 / (rate * 1_000_000.0),
                }
                for rate in link_rates
            ],
        }
        rows.append(row)

    baseline = rows[0]
    baseline_median = baseline["encode_median_s"]
    baseline_bytes = baseline["encoded_bytes"]
    baseline_link_totals = [
        link["encode_plus_wire_s"] for link in baseline["link_models"]
    ]
    for row in rows:
        row["encode_median_speedup_vs_baseline_x"] = (
            baseline_median / row["encode_median_s"]
        )
        row["encoded_size_reduction_vs_baseline_x"] = (
            baseline_bytes / row["encoded_bytes"]
        )
        for index, link in enumerate(row["link_models"]):
            link["encode_plus_wire_speedup_vs_baseline_x"] = (
                baseline_link_totals[index] / link["encode_plus_wire_s"]
            )

    ending_pins = current_code_pins()
    if ending_pins != starting_pins:
        raise PngTransportError("screen or tests changed during measurement")
    receipt: dict[str, Any] = {
        "claim_scope": CLAIM_SCOPE,
        "comparison_baseline_level": levels_list[0],
        "environment": environment,
        "evidence": EVIDENCE,
        "excluded": list(EXCLUDED),
        "kind": KIND,
        "pareto_compression_levels": pareto_levels(rows),
        "pins": starting_pins,
        "rows": rows,
        "schema_version": SCHEMA_VERSION,
        "source": source_record,
        "timing": {
            "clock": "time.perf_counter_ns",
            "order": "rotating_deterministic",
            "trial_count_per_level": trials,
            "warmup_per_level": 1,
        },
    }
    receipt["receipt_sha256"] = receipt_sha256(receipt)
    encoded = canonical_json(receipt)
    if len(encoded) > MAX_RECEIPT_BYTES:
        raise PngTransportError(f"receipt exceeds {MAX_RECEIPT_BYTES} bytes")
    validate_receipt(receipt, verify_external=False)
    return receipt


def _require_equal_float(value: Any, expected: float, label: str) -> float:
    observed = _finite(value, label, positive=expected > 0.0)
    if observed != expected:
        raise PngTransportError(f"{label} does not recompute from closed inputs")
    return observed


def _validate_environment(value: Any) -> dict[str, Any]:
    row = _exact_dict(value, _ENVIRONMENT_KEYS, "environment")
    for field in (
        "byteorder",
        "machine",
        "perf_counter_implementation",
        "pillow_version",
        "platform_release",
        "platform_system",
        "python_implementation",
        "python_version",
        "zlib_compile_version",
        "zlib_runtime_version",
    ):
        _bounded_text(row[field], f"environment.{field}", 1_024)
    _integer(row["cpu_count"], "environment.cpu_count", 0, 1_000_000)
    if type(row["perf_counter_adjustable"]) is not bool or type(
        row["perf_counter_monotonic"]
    ) is not bool:
        raise PngTransportError("environment perf-counter flags must be booleans")
    _finite(
        row["perf_counter_resolution_s"],
        "environment.perf_counter_resolution_s",
        positive=True,
    )
    if row != current_environment():
        raise PngTransportError("receipt environment does not match replay environment")
    return row


def _validate_pins(value: Any) -> dict[str, Any]:
    pins = _exact_dict(value, _PIN_KEYS, "pins")
    for name in sorted(_PIN_KEYS):
        record = _exact_dict(pins[name], _PIN_RECORD_KEYS, f"pins.{name}")
        _integer(record["bytes"], f"pins.{name}.bytes", 1, MAX_CODE_BYTES)
        path_text = _bounded_text(record["path"], f"pins.{name}.path")
        if not Path(path_text).is_absolute():
            raise PngTransportError(f"pins.{name}.path must be absolute")
        _sha256(record["sha256"], f"pins.{name}.sha256")
    if pins != current_code_pins():
        raise PngTransportError("receipt code pins do not match current implementation")
    return pins


def validate_receipt(
    receipt: dict[str, Any], *, verify_external: bool = True
) -> dict[str, Any]:
    """Replay a closed receipt, optionally including source and encoder bytes."""

    value = _exact_dict(receipt, _TOP_LEVEL_KEYS, "receipt")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != SCHEMA_VERSION
        or value["kind"] != KIND
    ):
        raise PngTransportError("receipt schema_version/kind mismatch")
    if value["claim_scope"] != CLAIM_SCOPE or value["evidence"] != EVIDENCE:
        raise PngTransportError("receipt claim scope/evidence changed")
    if value["excluded"] != list(EXCLUDED):
        raise PngTransportError("receipt exclusions changed")
    _sha256(value["receipt_sha256"], "receipt.receipt_sha256")
    if receipt_sha256(value) != value["receipt_sha256"]:
        raise PngTransportError("receipt self SHA is invalid")
    _validate_environment(value["environment"])
    _validate_pins(value["pins"])

    source = _exact_dict(value["source"], _SOURCE_KEYS, "source")
    source_bytes = _integer(source["bytes"], "source.bytes", 1, MAX_SOURCE_BYTES)
    width = _integer(source["width"], "source.width", 1, MAX_DIMENSION)
    height = _integer(source["height"], "source.height", 1, MAX_DIMENSION)
    if width * height > MAX_PIXELS:
        raise PngTransportError("source dimensions exceed the pixel bound")
    frame_count = _integer(source["frame_count"], "source.frame_count", 1, 1)
    decoded_rgba_bytes = _integer(
        source["decoded_rgba_bytes"],
        "source.decoded_rgba_bytes",
        4,
        4 * MAX_PIXELS,
    )
    if source["decoded_mode"] != "RGBA" or frame_count != 1:
        raise PngTransportError("source mode/frame contract changed")
    if decoded_rgba_bytes != width * height * 4:
        raise PngTransportError("source decoded byte count is inconsistent")
    source_rgba_sha = _sha256(
        source["decoded_rgba_sha256"], "source.decoded_rgba_sha256"
    )
    _sha256(source["sha256"], "source.sha256")
    source_path_text = _bounded_text(source["path"], "source.path")
    if not Path(source_path_text).is_absolute():
        raise PngTransportError("source.path must be absolute")
    _integer(source["source_device"], "source.source_device", 0, (1 << 128) - 1)
    _integer(source["source_inode"], "source.source_inode", 0, (1 << 128) - 1)

    timing = _exact_dict(value["timing"], _TIMING_KEYS, "timing")
    trials = _integer(
        timing["trial_count_per_level"], "timing.trial_count_per_level", 3, 31
    )
    if trials % 2 == 0:
        raise PngTransportError("timing trial count must be odd")
    if (
        timing["clock"] != "time.perf_counter_ns"
        or timing["order"] != "rotating_deterministic"
        or _integer(timing["warmup_per_level"], "timing.warmup_per_level", 1, 1)
        != 1
    ):
        raise PngTransportError("receipt timing protocol changed")

    rows = value["rows"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 10:
        raise PngTransportError("receipt rows must contain 1..10 levels")
    normalized_rows: list[dict[str, Any]] = []
    levels: list[int] = []
    all_rates: list[list[float]] = []
    for index, candidate in enumerate(rows):
        row = _exact_dict(candidate, _ROW_KEYS, f"rows[{index}]")
        level = _integer(row["compress_level"], f"rows[{index}].compress_level", 0, 9)
        levels.append(level)
        if row["decoded_rgba_exact"] is not True:
            raise PngTransportError(f"rows[{index}] is not decoded-RGBA exact")
        if row["decoded_rgba_sha256"] != source_rgba_sha:
            raise PngTransportError(f"rows[{index}] decoded identity contradicts source")
        encoded_bytes = _integer(
            row["encoded_bytes"], f"rows[{index}].encoded_bytes", 1, MAX_ENCODED_BYTES
        )
        encoded_sha = _sha256(row["encoded_sha256"], f"rows[{index}].encoded_sha256")
        if row["encoded_deterministic_across_trials"] is not True:
            raise PngTransportError(f"rows[{index}] is not deterministic")
        samples_value = row["encode_samples_s"]
        if not isinstance(samples_value, list) or len(samples_value) != trials:
            raise PngTransportError(f"rows[{index}] sample count contradicts timing")
        samples = [
            _finite(sample, f"rows[{index}].encode_samples_s[{sample_index}]", positive=True)
            for sample_index, sample in enumerate(samples_value)
        ]
        median_s = statistics.median(samples)
        _require_equal_float(row["encode_median_s"], median_s, f"rows[{index}].encode_median_s")
        _require_equal_float(
            row["encode_p95_s_type7"],
            type7(samples, 0.95),
            f"rows[{index}].encode_p95_s_type7",
        )
        identities = row["encoded_trial_identities"]
        if not isinstance(identities, list) or len(identities) != trials:
            raise PngTransportError(f"rows[{index}] trial identities contradict timing")
        for trial_index, identity in enumerate(identities):
            identity = _exact_dict(
                identity,
                _TRIAL_IDENTITY_KEYS,
                f"rows[{index}].encoded_trial_identities[{trial_index}]",
            )
            trial_bytes = _integer(
                identity["bytes"],
                f"rows[{index}].encoded_trial_identities[{trial_index}].bytes",
                1,
                MAX_ENCODED_BYTES,
            )
            trial_sha = _sha256(
                identity["sha256"],
                f"rows[{index}].encoded_trial_identities[{trial_index}].sha256",
            )
            if trial_bytes != encoded_bytes or trial_sha != encoded_sha:
                raise PngTransportError(
                    f"rows[{index}] encoded trial identity is not deterministic"
                )

        links = row["link_models"]
        if not isinstance(links, list) or not 1 <= len(links) <= MAX_LINK_RATES:
            raise PngTransportError(f"rows[{index}] link models are outside the bound")
        rates: list[float] = []
        for link_index, link_value in enumerate(links):
            link = _exact_dict(
                link_value, _LINK_MODEL_KEYS, f"rows[{index}].link_models[{link_index}]"
            )
            rate = _finite(
                link["link_mbps_decimal"],
                f"rows[{index}].link_models[{link_index}].link_mbps_decimal",
                positive=True,
            )
            if rate > 1_000_000.0:
                raise PngTransportError("receipt link rate exceeds its closed bound")
            rates.append(rate)
            wire_s = encoded_bytes * 8.0 / (rate * 1_000_000.0)
            _require_equal_float(
                link["wire_floor_s"],
                wire_s,
                f"rows[{index}].link_models[{link_index}].wire_floor_s",
            )
            _require_equal_float(
                link["encode_plus_wire_s"],
                median_s + wire_s,
                f"rows[{index}].link_models[{link_index}].encode_plus_wire_s",
            )
        if rates != validate_link_rates(rates):
            raise PngTransportError(f"rows[{index}] link rates are not canonical")
        all_rates.append(rates)
        normalized_rows.append(row)

    if levels != validate_levels(levels):
        raise PngTransportError("receipt compression levels are not canonical")
    if any(rates != all_rates[0] for rates in all_rates[1:]):
        raise PngTransportError("receipt rows use different link-rate grids")
    baseline_level = _integer(
        value["comparison_baseline_level"], "comparison_baseline_level", 0, 9
    )
    if baseline_level != levels[0]:
        raise PngTransportError("comparison baseline must be the first canonical level")
    baseline = normalized_rows[0]
    baseline_median = float(baseline["encode_median_s"])
    baseline_bytes = baseline["encoded_bytes"]
    baseline_totals = [
        float(link["encode_plus_wire_s"]) for link in baseline["link_models"]
    ]
    for row_index, row in enumerate(normalized_rows):
        _require_equal_float(
            row["encode_median_speedup_vs_baseline_x"],
            baseline_median / float(row["encode_median_s"]),
            f"rows[{row_index}].encode_median_speedup_vs_baseline_x",
        )
        _require_equal_float(
            row["encoded_size_reduction_vs_baseline_x"],
            baseline_bytes / row["encoded_bytes"],
            f"rows[{row_index}].encoded_size_reduction_vs_baseline_x",
        )
        for link_index, link in enumerate(row["link_models"]):
            _require_equal_float(
                link["encode_plus_wire_speedup_vs_baseline_x"],
                baseline_totals[link_index] / float(link["encode_plus_wire_s"]),
                f"rows[{row_index}].link_models[{link_index}].encode_plus_wire_speedup_vs_baseline_x",
            )

    pareto = value["pareto_compression_levels"]
    if (
        not isinstance(pareto, list)
        or any(type(level) is not int for level in pareto)
        or pareto != pareto_levels(normalized_rows)
    ):
        raise PngTransportError("receipt Pareto levels do not recompute")

    if verify_external:
        image, actual_source = load_png(Path(source_path_text))
        if actual_source != source or actual_source["bytes"] != source_bytes:
            raise PngTransportError("receipt source identity does not replay")
        for row_index, row in enumerate(normalized_rows):
            first, _ = encode_png(image, row["compress_level"])
            second, _ = encode_png(image, row["compress_level"])
            if first != second:
                raise PngTransportError("PNG encoder is not deterministic during replay")
            if (
                len(first) != row["encoded_bytes"]
                or sha256_bytes(first) != row["encoded_sha256"]
                or decoded_rgba_sha256(first) != source_rgba_sha
            ):
                raise PngTransportError(
                    f"rows[{row_index}] encoded candidate identity does not replay"
                )
    return value


def validate_receipt_bytes(raw: bytes, *, verify_external: bool = True) -> dict[str, Any]:
    return validate_receipt(parse_receipt(raw), verify_external=verify_external)


def validate_receipt_path(path: Path, *, verify_external: bool = True) -> dict[str, Any]:
    raw = _read_bounded_regular_file(path, MAX_RECEIPT_BYTES, "receipt")
    return validate_receipt_bytes(raw, verify_external=verify_external)


def publish_no_clobber(path: Path, payload: bytes) -> None:
    if not path.name or path.name in {".", ".."}:
        raise PngTransportError("output must name a file")
    try:
        parent_info = path.parent.lstat()
    except OSError as exc:
        raise PngTransportError("output parent is unavailable") from exc
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise PngTransportError("output parent must be an existing real directory")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(path.parent, directory_flags)
    except OSError as exc:
        raise PngTransportError("output parent failed no-follow open") from exc
    opened_parent = os.fstat(directory_fd)
    if (parent_info.st_dev, parent_info.st_ino) != (
        opened_parent.st_dev,
        opened_parent.st_ino,
    ):
        os.close(directory_fd)
        raise PngTransportError("output parent identity changed")
    stage_name = f".{path.name}.{secrets.token_hex(16)}.stage"
    descriptor: int | None = None
    linked = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(stage_name, flags, 0o600, dir_fd=directory_fd)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise PngTransportError("short output write")
            view = view[written:]
        os.fsync(descriptor)
        staged_info = os.fstat(descriptor)
        os.link(
            stage_name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        linked = True
        os.fsync(directory_fd)
        published = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            published.st_dev,
            published.st_ino,
            published.st_size,
        ) != (staged_info.st_dev, staged_info.st_ino, len(payload)):
            raise PngTransportError("published output changed identity")
        os.unlink(stage_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except FileExistsError as exc:
        raise PngTransportError("output already exists") from exc
    except OSError as exc:
        if linked:
            try:
                os.unlink(path.name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except OSError:
                pass
        raise PngTransportError("durable output publication failed") from exc
    except BaseException:
        if linked:
            try:
                os.unlink(path.name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except OSError:
                pass
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(stage_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(directory_fd)


def _parse_csv_ints(raw: str) -> list[int]:
    try:
        return [int(part.strip()) for part in raw.split(",")]
    except ValueError as exc:
        raise PngTransportError("compression levels contain a non-integer") from exc


def _parse_csv_floats(raw: str) -> list[float]:
    try:
        return [float(part.strip()) for part in raw.split(",")]
    except ValueError as exc:
        raise PngTransportError("link rates contain a non-number") from exc


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source", type=Path)
    source.add_argument(
        "--validate-receipt",
        type=Path,
        help="strictly replay one existing closed receipt",
    )
    parser.add_argument("--levels", default=",".join(map(str, DEFAULT_LEVELS)))
    parser.add_argument("--trials", default=7, type=int)
    parser.add_argument(
        "--link-rates-mbps", default=",".join(map(str, DEFAULT_LINK_MBPS))
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.validate_receipt is not None:
            if args.output is not None:
                raise PngTransportError("receipt validation cannot publish an output")
            receipt = validate_receipt_path(args.validate_receipt)
            proof = {
                "kind": KIND,
                "ok": True,
                "receipt_sha256": receipt["receipt_sha256"],
            }
            sys.stdout.buffer.write(canonical_json(proof, pretty=args.pretty))
            return 0
        receipt = run_screen(
            args.source,
            levels=_parse_csv_ints(args.levels),
            trials=args.trials,
            link_rates_mbps=_parse_csv_floats(args.link_rates_mbps),
        )
        payload = canonical_json(receipt, pretty=args.pretty)
        if len(payload) > MAX_RECEIPT_BYTES:
            raise PngTransportError(f"receipt exceeds {MAX_RECEIPT_BYTES} bytes")
        if args.output is not None:
            publish_no_clobber(args.output, payload)
        sys.stdout.buffer.write(payload)
        return 0
    except Exception as exc:
        sys.stderr.buffer.write(
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
