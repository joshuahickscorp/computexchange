#!/usr/bin/env python3
"""Deterministic preview-quality contract v3 for rendered PNG artifacts.

The authoritative comparison is a frozen candidate against a high-sample
reference.  This module intentionally retains the legacy display-RGB L1 grids
while adding alpha-safe, structural, gradient, noise, and native-detail gates.
Every floating-point value is rounded to the nine-decimal wire representation
*before* its threshold is evaluated.

The public :func:`evaluate_pngs` API is fail closed: malformed inputs and
unavailable/non-finite metrics produce a structured rejection instead of an
exception.  The command-line interface writes that same deterministic JSON.
"""

from __future__ import annotations

import argparse
from functools import lru_cache
import hashlib
from io import BytesIO
import importlib
import json
import math
from pathlib import Path
import struct
import sys
from typing import Any, Iterable, Sequence
import zlib

import numpy as np
import PIL
from PIL import Image, UnidentifiedImageError
import scipy
from scipy.ndimage import gaussian_filter, sobel


CONTRACT_ID = "cx-render-preview-quality-v3"
RESULT_KIND = "cx_render_quality_v3_result"
SCHEMA_VERSION = 3
WIRE_DECIMALS = 9
MAX_PIXELS = 100_000_000
MAX_PNG_BYTES = 512 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

ALPHA_AGREEMENT_MIN = 0.997
REFERENCE_LUMA_STD_MIN = 0.10
REFERENCE_EDGE_ACTIVE_FRACTION_MIN = 0.10
REFERENCE_EDGE_ACTIVE_MAGNITUDE = 0.01
GLOBAL_RGB_AGREEMENT_MIN = 0.94
REGIONAL_RGB_AGREEMENT_MIN = 0.85
MICROTILE_RGB_AGREEMENT_MIN = 0.70
ACTIVITY_STRATUM_RGB_AGREEMENT_MIN = 0.90
SSIM_MIN = 0.70
SSIM_REGIONAL_P5_MIN = 0.40
GMS_MEAN_MIN = 0.90
GMSD_MAX = 0.15
FLAT_HIGH_PASS_RMSE_MAX = 0.030
GRADIENT_ENERGY_RATIO_MIN = 0.50
GRADIENT_ENERGY_RATIO_MAX = 2.00
HAAR_DETAIL_COSINE_MIN = 0.40
HAAR_DETAIL_GAIN_MIN = 0.90
HAAR_DETAIL_GAIN_MAX = 2.00

AGREEMENT_MIN_TILE_EDGE = 32
AGREEMENT_MAX_LONG_EDGE_TILES = 16
MICROTILE_EDGE = 32
SSIM_SIGMA = 1.5
SSIM_RADIUS = 5
SSIM_C1 = 0.01**2
SSIM_C2 = 0.03**2
GMS_T = 170.0 / (255.0**2)
HIGH_PASS_SIGMA = 1.0
HIGH_PASS_RADIUS = 4
LUMA_WEIGHTS = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)
MATTE_VALUES = (("black", 0.0), ("white", 1.0))
ACTIVITY_STRATA = (
    ("p00_p50", 0.00, 0.50),
    ("p50_p75", 0.50, 0.75),
    ("p75_p90", 0.75, 0.90),
    ("p90_p100", 0.90, 1.00),
)


CONTRACT_DESCRIPTOR: dict[str, Any] = {
    "id": CONTRACT_ID,
    "wire_decimals": WIRE_DECIMALS,
    "input": {
        "media_type": "image/png",
        "png_bit_depth": 8,
        "png_color_types": [2, 6],
        "decoded_modes": ["RGB", "RGBA"],
        "exact_target_dimensions": True,
    },
    "alpha": {
        "rgb_implies_opaque_alpha": True,
        "agreement_minimum": ALPHA_AGREEMENT_MIN,
        "straight_alpha_mattes": [0.0, 1.0],
    },
    "reference_eligibility": {
        "luma_std_minimum": REFERENCE_LUMA_STD_MIN,
        "sobel_active_fraction_minimum": REFERENCE_EDGE_ACTIVE_FRACTION_MIN,
        "sobel_active_magnitude_exclusive": REFERENCE_EDGE_ACTIVE_MAGNITUDE,
    },
    "thresholds": {
        "global_rgb_agreement_minimum": GLOBAL_RGB_AGREEMENT_MIN,
        "worst_regional_rgb_agreement_minimum": REGIONAL_RGB_AGREEMENT_MIN,
        "worst_microtile_rgb_agreement_minimum": MICROTILE_RGB_AGREEMENT_MIN,
        "activity_stratum_rgb_agreement_minimum": ACTIVITY_STRATUM_RGB_AGREEMENT_MIN,
        "gaussian_luma_ssim_minimum": SSIM_MIN,
        "regional_ssim_p5_minimum": SSIM_REGIONAL_P5_MIN,
        "sobel_gms_mean_minimum": GMS_MEAN_MIN,
        "sobel_gmsd_maximum": GMSD_MAX,
        "flat_high_pass_rmse_maximum": FLAT_HIGH_PASS_RMSE_MAX,
        "sobel_gradient_energy_ratio": [
            GRADIENT_ENERGY_RATIO_MIN,
            GRADIENT_ENERGY_RATIO_MAX,
        ],
        "haar_detail_cosine_minimum": HAAR_DETAIL_COSINE_MIN,
        "haar_detail_rms_gain": [HAAR_DETAIL_GAIN_MIN, HAAR_DETAIL_GAIN_MAX],
    },
    "algorithms": {
        "luma": "display-code Rec.709 weights 0.2126/0.7152/0.0722",
        "sobel": "scipy.ndimage.sobel reflect boundaries divided by 8",
        "activity_order": "ascending reference Sobel magnitude, stable row-major ties",
        "ssim": "direct 11x11 Gaussian sigma=1.5 population-covariance map, valid interior",
        "ssim_quantile": "type-7 linear p5 over balanced regional map means",
        "gms": "Sobel gradient-magnitude similarity with T=170/255^2",
        "high_pass": "luma minus Gaussian sigma=1 radius=4 on lowest activity half",
        "haar": "native non-overlapping 2x2 block-mean residual on top activity decile",
    },
}


class QualityInputError(ValueError):
    """A stable-code artifact input error."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class MetricUnavailable(ValueError):
    """A deterministic metric cannot be calculated for this input."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


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


def _wire(value: float) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise MetricUnavailable("nonfinite_metric")
    rounded = round(number, WIRE_DECIMALS)
    return 0.0 if rounded == 0.0 else rounded


def _minimum_metric(value: float | None, minimum: float) -> dict[str, Any]:
    if value is None:
        return {"value": None, "minimum": minimum, "pass": False}
    try:
        emitted = _wire(value)
    except MetricUnavailable:
        return {"value": None, "minimum": minimum, "pass": False}
    return {"value": emitted, "minimum": minimum, "pass": emitted >= minimum}


def _maximum_metric(value: float | None, maximum: float) -> dict[str, Any]:
    if value is None:
        return {"value": None, "maximum": maximum, "pass": False}
    try:
        emitted = _wire(value)
    except MetricUnavailable:
        return {"value": None, "maximum": maximum, "pass": False}
    return {"value": emitted, "maximum": maximum, "pass": emitted <= maximum}


def _range_metric(
    value: float | None, minimum: float, maximum: float
) -> dict[str, Any]:
    if value is None:
        return {
            "value": None,
            "minimum": minimum,
            "maximum": maximum,
            "pass": False,
        }
    try:
        emitted = _wire(value)
    except MetricUnavailable:
        return {
            "value": None,
            "minimum": minimum,
            "maximum": maximum,
            "pass": False,
        }
    return {
        "value": emitted,
        "minimum": minimum,
        "maximum": maximum,
        "pass": minimum <= emitted <= maximum,
    }


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
    compiled = {
        "PIL._imaging": _compiled_module_record("PIL._imaging"),
        "numpy._core._multiarray_umath": _compiled_module_record(
            "numpy._core._multiarray_umath"
        ),
        "scipy.ndimage._nd_image": _compiled_module_record(
            "scipy.ndimage._nd_image"
        ),
    }
    versions = {
        "pillow": PIL.__version__,
        "numpy": np.__version__,
        "scipy": scipy.__version__,
    }
    tree = {"versions": versions, "compiled_modules": compiled}
    module_path = Path(__file__).resolve()
    return {
        **tree,
        "dependency_tree_sha256": _sha256_bytes(_canonical_json(tree)),
        "metric_module_sha256": _sha256_file(module_path),
    }


def _read_png_bytes(path: str | Path) -> bytes:
    try:
        value = Path(path).read_bytes()
    except OSError as exc:
        raise QualityInputError("unreadable_png") from exc
    if len(value) > MAX_PNG_BYTES:
        raise QualityInputError("png_too_large")
    return value


def _validate_png_container(
    data: bytes, target_size: tuple[int, int]
) -> tuple[int, str]:
    if not data.startswith(PNG_SIGNATURE):
        raise QualityInputError("invalid_png_signature")
    offset = len(PNG_SIGNATURE)
    chunk_index = 0
    saw_ihdr = False
    saw_idat = False
    saw_iend = False
    idat_ended = False
    expected_mode: str | None = None
    while offset < len(data):
        if len(data) - offset < 12:
            raise QualityInputError("truncated_png_chunk")
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise QualityInputError("truncated_png_chunk")
        payload = data[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", data[offset + 8 + length : end])[0]
        actual_crc = zlib.crc32(chunk_type)
        actual_crc = zlib.crc32(payload, actual_crc) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise QualityInputError("png_crc_mismatch")
        if chunk_index == 0 and chunk_type != b"IHDR":
            raise QualityInputError("png_ihdr_not_first")
        if chunk_type == b"IHDR":
            if saw_ihdr or length != 13:
                raise QualityInputError("invalid_png_ihdr")
            width, height, bit_depth, color_type, compression, filtering, _interlace = (
                struct.unpack(">IIBBBBB", payload)
            )
            if (width, height) != target_size:
                raise QualityInputError("png_dimension_mismatch")
            if bit_depth != 8:
                raise QualityInputError("png_bit_depth_not_8")
            if color_type not in (2, 6):
                raise QualityInputError("png_color_type_not_rgb_or_rgba")
            if compression != 0 or filtering != 0 or _interlace != 0:
                raise QualityInputError("unsupported_png_method")
            expected_mode = "RGB" if color_type == 2 else "RGBA"
            saw_ihdr = True
        elif chunk_type == b"IDAT":
            if not saw_ihdr or saw_iend or idat_ended:
                raise QualityInputError("invalid_png_idat_order")
            saw_idat = True
        elif chunk_type == b"IEND":
            if length != 0 or not saw_idat or saw_iend:
                raise QualityInputError("invalid_png_iend")
            saw_iend = True
            if end != len(data):
                raise QualityInputError("png_trailing_data")
        else:
            if saw_idat:
                idat_ended = True
            # Unknown critical chunks have an uppercase first type byte.
            if chunk_type[:1].isalpha() and chunk_type[:1].isupper() and chunk_type not in {
                b"PLTE",
            }:
                raise QualityInputError("unknown_png_critical_chunk")
        offset = end
        chunk_index += 1
        if saw_iend:
            break
    if not (saw_ihdr and saw_idat and saw_iend) or expected_mode is None:
        raise QualityInputError("incomplete_png")
    return 3 if expected_mode == "RGB" else 4, expected_mode


def _decode_png(
    data: bytes, target_size: tuple[int, int]
) -> tuple[np.ndarray, np.ndarray, str]:
    channels, expected_mode = _validate_png_container(data, target_size)
    try:
        with Image.open(BytesIO(data)) as source:
            if source.format != "PNG" or source.mode != expected_mode:
                raise QualityInputError("png_decoder_mode_mismatch")
            if getattr(source, "n_frames", 1) != 1:
                raise QualityInputError("animated_png_not_supported")
            source.load()
            array = np.asarray(source, dtype=np.uint8)
    except QualityInputError:
        raise
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise QualityInputError("png_decode_failed") from exc
    height = target_size[1]
    width = target_size[0]
    if array.shape != (height, width, channels):
        raise QualityInputError("decoded_shape_mismatch")
    values = array.astype(np.float64) / 255.0
    rgb = values[..., :3]
    alpha = values[..., 3] if channels == 4 else np.ones((height, width))
    if not (np.isfinite(rgb).all() and np.isfinite(alpha).all()):
        raise QualityInputError("nonfinite_decoded_values")
    return rgb, alpha, expected_mode


def _validate_target_size(target_size: Sequence[int]) -> tuple[int, int]:
    if (
        len(target_size) != 2
        or isinstance(target_size[0], bool)
        or isinstance(target_size[1], bool)
        or not isinstance(target_size[0], int)
        or not isinstance(target_size[1], int)
    ):
        raise QualityInputError("invalid_target_dimensions")
    width, height = int(target_size[0]), int(target_size[1])
    if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
        raise QualityInputError("invalid_target_dimensions")
    return width, height


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


def _tiles(
    width: int, height: int, columns: int, rows: int
) -> Iterable[tuple[int, int, int, int]]:
    for row in range(rows):
        top = row * height // rows
        bottom = (row + 1) * height // rows
        for column in range(columns):
            left = column * width // columns
            right = (column + 1) * width // columns
            yield left, top, right, bottom


def _luma(rgb: np.ndarray) -> np.ndarray:
    return np.sum(rgb * LUMA_WEIGHTS, axis=2, dtype=np.float64)


def _sobel_components(luma: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    gx = sobel(luma, axis=1, mode="reflect") / 8.0
    gy = sobel(luma, axis=0, mode="reflect") / 8.0
    magnitude = np.hypot(gx, gy)
    return gx, gy, magnitude


def _stable_activity_order(magnitude: np.ndarray) -> np.ndarray:
    if magnitude.ndim != 2 or not np.isfinite(magnitude).all():
        raise MetricUnavailable("invalid_reference_activity")
    return np.argsort(magnitude.ravel(), kind="stable")


def _activity_slices(order: np.ndarray) -> dict[str, np.ndarray]:
    count = int(order.size)
    values: dict[str, np.ndarray] = {}
    for label, lower, upper in ACTIVITY_STRATA:
        start = int(lower * count)
        end = int(upper * count)
        selected = order[start:end]
        if selected.size == 0:
            raise MetricUnavailable(f"empty_activity_stratum:{label}")
        values[label] = selected
    return values


def _rgb_agreement_values(
    candidate: np.ndarray, reference: np.ndarray, size: tuple[int, int]
) -> tuple[float, float, float, np.ndarray]:
    difference = np.abs(candidate - reference)
    if not np.isfinite(difference).all():
        raise MetricUnavailable("nonfinite_rgb_difference")
    pixel_sum = np.sum(difference, axis=2, dtype=np.float64)
    height, width = pixel_sum.shape
    integral = np.zeros((height + 1, width + 1), dtype=np.float64)
    integral[1:, 1:] = np.cumsum(
        np.cumsum(pixel_sum, axis=0, dtype=np.float64),
        axis=1,
        dtype=np.float64,
    )

    def box_score(box: tuple[int, int, int, int]) -> float:
        left, top, right, bottom = box
        total = (
            integral[bottom, right]
            - integral[top, right]
            - integral[bottom, left]
            + integral[top, left]
        )
        pixels = (right - left) * (bottom - top)
        if pixels <= 0:
            raise MetricUnavailable("empty_agreement_tile")
        return 1.0 - float(total) / (pixels * 3.0)

    regional = _agreement_grid(size)
    micro = _microtile_grid(size)
    regional_values = [
        box_score(box) for box in _tiles(width, height, *regional)
    ]
    micro_values = [box_score(box) for box in _tiles(width, height, *micro)]
    if not regional_values or not micro_values:
        raise MetricUnavailable("empty_agreement_grid")
    return (
        1.0 - float(np.mean(pixel_sum, dtype=np.float64)) / 3.0,
        min(regional_values),
        min(micro_values),
        difference.reshape(-1, 3),
    )


def _ssim_map(candidate: np.ndarray, reference: np.ndarray) -> np.ndarray:
    mu_c = gaussian_filter(
        candidate, sigma=SSIM_SIGMA, radius=SSIM_RADIUS, mode="reflect"
    )
    mu_r = gaussian_filter(
        reference, sigma=SSIM_SIGMA, radius=SSIM_RADIUS, mode="reflect"
    )
    e_cc = gaussian_filter(
        candidate * candidate,
        sigma=SSIM_SIGMA,
        radius=SSIM_RADIUS,
        mode="reflect",
    )
    e_rr = gaussian_filter(
        reference * reference,
        sigma=SSIM_SIGMA,
        radius=SSIM_RADIUS,
        mode="reflect",
    )
    e_cr = gaussian_filter(
        candidate * reference,
        sigma=SSIM_SIGMA,
        radius=SSIM_RADIUS,
        mode="reflect",
    )
    var_c = np.maximum(0.0, e_cc - mu_c * mu_c)
    var_r = np.maximum(0.0, e_rr - mu_r * mu_r)
    covariance = e_cr - mu_c * mu_r
    numerator = (2.0 * mu_c * mu_r + SSIM_C1) * (
        2.0 * covariance + SSIM_C2
    )
    denominator = (mu_c * mu_c + mu_r * mu_r + SSIM_C1) * (
        var_c + var_r + SSIM_C2
    )
    if np.any(denominator == 0.0):
        raise MetricUnavailable("zero_ssim_denominator")
    result = numerator / denominator
    if not np.isfinite(result).all():
        raise MetricUnavailable("nonfinite_ssim_map")
    return result


def _type7_quantile(values: Sequence[float], quantile: float) -> float:
    if not values:
        raise MetricUnavailable("empty_quantile")
    ordered = sorted(float(value) for value in values)
    if not all(math.isfinite(value) for value in ordered):
        raise MetricUnavailable("nonfinite_quantile")
    h = (len(ordered) - 1) * quantile
    lower = math.floor(h)
    upper = math.ceil(h)
    fraction = h - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _ssim_values(
    candidate_luma: np.ndarray,
    reference_luma: np.ndarray,
    size: tuple[int, int],
) -> tuple[float, float]:
    height, width = candidate_luma.shape
    if height <= 2 * SSIM_RADIUS or width <= 2 * SSIM_RADIUS:
        raise MetricUnavailable("image_too_small_for_ssim")
    score_map = _ssim_map(candidate_luma, reference_luma)
    valid = score_map[
        SSIM_RADIUS : height - SSIM_RADIUS,
        SSIM_RADIUS : width - SSIM_RADIUS,
    ]
    if valid.size == 0:
        raise MetricUnavailable("empty_ssim_interior")
    columns, rows = _agreement_grid(size)
    regional_values: list[float] = []
    for left, top, right, bottom in _tiles(width, height, columns, rows):
        valid_left = max(left, SSIM_RADIUS)
        valid_top = max(top, SSIM_RADIUS)
        valid_right = min(right, width - SSIM_RADIUS)
        valid_bottom = min(bottom, height - SSIM_RADIUS)
        if valid_left >= valid_right or valid_top >= valid_bottom:
            raise MetricUnavailable("empty_regional_ssim_interior")
        regional_values.append(
            float(
                np.mean(
                    score_map[
                        valid_top:valid_bottom,
                        valid_left:valid_right,
                    ],
                    dtype=np.float64,
                )
            )
        )
    return float(np.mean(valid, dtype=np.float64)), _type7_quantile(
        regional_values, 0.05
    )


def _haar_values(
    candidate_luma: np.ndarray,
    reference_luma: np.ndarray,
    reference_magnitude: np.ndarray,
) -> tuple[float, float]:
    height, width = reference_luma.shape
    even_height = height - height % 2
    even_width = width - width % 2
    if even_height < 2 or even_width < 2:
        raise MetricUnavailable("image_too_small_for_haar")
    candidate = candidate_luma[:even_height, :even_width]
    reference = reference_luma[:even_height, :even_width]
    activity = reference_magnitude[:even_height, :even_width]

    def detail(values: np.ndarray) -> np.ndarray:
        blocks = values.reshape(even_height // 2, 2, even_width // 2, 2)
        means = np.mean(blocks, axis=(1, 3), dtype=np.float64)
        expanded = np.repeat(np.repeat(means, 2, axis=0), 2, axis=1)
        return values - expanded

    candidate_detail = detail(candidate).ravel()
    reference_detail = detail(reference).ravel()
    order = _stable_activity_order(activity)
    start = int(0.90 * order.size)
    selected = order[start:]
    if selected.size == 0:
        raise MetricUnavailable("empty_haar_activity_decile")
    candidate_selected = candidate_detail[selected]
    reference_selected = reference_detail[selected]
    candidate_norm = float(np.linalg.norm(candidate_selected))
    reference_norm = float(np.linalg.norm(reference_selected))
    if candidate_norm == 0.0 or reference_norm == 0.0:
        raise MetricUnavailable("zero_haar_norm")
    cosine = float(
        np.dot(candidate_selected, reference_selected)
        / (candidate_norm * reference_norm)
    )
    reference_mean_square = float(
        np.mean(reference_selected * reference_selected, dtype=np.float64)
    )
    if reference_mean_square == 0.0:
        raise MetricUnavailable("zero_haar_reference_energy")
    gain = math.sqrt(
        float(np.mean(candidate_selected * candidate_selected, dtype=np.float64))
        / reference_mean_square
    )
    return cosine, gain


def _unavailable_matte(
    size: tuple[int, int], reason: str
) -> dict[str, Any]:
    regional = _agreement_grid(size)
    micro = _microtile_grid(size)
    unavailable_minimums = {
        "global_rgb_agreement": _minimum_metric(None, GLOBAL_RGB_AGREEMENT_MIN),
        "worst_regional_rgb_agreement": _minimum_metric(
            None, REGIONAL_RGB_AGREEMENT_MIN
        ),
        "worst_microtile_rgb_agreement": _minimum_metric(
            None, MICROTILE_RGB_AGREEMENT_MIN
        ),
        "gaussian_luma_ssim": _minimum_metric(None, SSIM_MIN),
        "regional_ssim_p5": _minimum_metric(None, SSIM_REGIONAL_P5_MIN),
        "sobel_gms_mean": _minimum_metric(None, GMS_MEAN_MIN),
        "sobel_gmsd": _maximum_metric(None, GMSD_MAX),
        "flat_high_pass_rmse": _maximum_metric(None, FLAT_HIGH_PASS_RMSE_MAX),
        "sobel_gradient_energy_ratio": _range_metric(
            None, GRADIENT_ENERGY_RATIO_MIN, GRADIENT_ENERGY_RATIO_MAX
        ),
        "haar_detail_cosine": _minimum_metric(None, HAAR_DETAIL_COSINE_MIN),
        "haar_detail_rms_gain": _range_metric(
            None, HAAR_DETAIL_GAIN_MIN, HAAR_DETAIL_GAIN_MAX
        ),
    }
    activity = {
        label: _minimum_metric(None, ACTIVITY_STRATUM_RGB_AGREEMENT_MIN)
        for label, _lower, _upper in ACTIVITY_STRATA
    }
    return {
        "reference_eligibility": {
            "luma_std": _minimum_metric(None, REFERENCE_LUMA_STD_MIN),
            "sobel_active_fraction": _minimum_metric(
                None, REFERENCE_EDGE_ACTIVE_FRACTION_MIN
            ),
            "sobel_active_magnitude_exclusive": REFERENCE_EDGE_ACTIVE_MAGNITUDE,
            "pass": False,
        },
        "grid": {
            "regional": {
                "columns": regional[0],
                "rows": regional[1],
                "count": regional[0] * regional[1],
            },
            "microtile": {
                "columns": micro[0],
                "rows": micro[1],
                "count": micro[0] * micro[1],
                "nominal_edge_pixels": MICROTILE_EDGE,
            },
        },
        "metrics": {
            **unavailable_minimums,
            "activity_strata_rgb_agreement": activity,
        },
        "unavailable_reasons": [reason],
        "pass": False,
    }


def _evaluate_matte(
    candidate: np.ndarray,
    reference: np.ndarray,
    size: tuple[int, int],
) -> dict[str, Any]:
    if (
        candidate.shape != reference.shape
        or candidate.ndim != 3
        or candidate.shape[2] != 3
        or not np.isfinite(candidate).all()
        or not np.isfinite(reference).all()
    ):
        return _unavailable_matte(size, "invalid_or_nonfinite_composite")
    try:
        candidate_luma = _luma(candidate)
        reference_luma = _luma(reference)
        candidate_gx, candidate_gy, candidate_magnitude = _sobel_components(
            candidate_luma
        )
        reference_gx, reference_gy, reference_magnitude = _sobel_components(
            reference_luma
        )
        order = _stable_activity_order(reference_magnitude)
        strata = _activity_slices(order)
        global_score, regional_score, micro_score, flat_difference = (
            _rgb_agreement_values(candidate, reference, size)
        )
        activity_metrics = {
            label: _minimum_metric(
                1.0
                - float(
                    np.mean(flat_difference[selected], dtype=np.float64)
                ),
                ACTIVITY_STRATUM_RGB_AGREEMENT_MIN,
            )
            for label, selected in strata.items()
        }
        ssim, ssim_p5 = _ssim_values(candidate_luma, reference_luma, size)
        gms_map = (
            2.0 * candidate_magnitude * reference_magnitude + GMS_T
        ) / (
            candidate_magnitude * candidate_magnitude
            + reference_magnitude * reference_magnitude
            + GMS_T
        )
        if not np.isfinite(gms_map).all():
            raise MetricUnavailable("nonfinite_gms")
        gms_mean = float(np.mean(gms_map, dtype=np.float64))
        gmsd = float(np.std(gms_map, ddof=0, dtype=np.float64))

        candidate_high_pass = candidate_luma - gaussian_filter(
            candidate_luma,
            sigma=HIGH_PASS_SIGMA,
            radius=HIGH_PASS_RADIUS,
            mode="reflect",
        )
        reference_high_pass = reference_luma - gaussian_filter(
            reference_luma,
            sigma=HIGH_PASS_SIGMA,
            radius=HIGH_PASS_RADIUS,
            mode="reflect",
        )
        flat_indices = order[: int(0.50 * order.size)]
        if flat_indices.size == 0:
            raise MetricUnavailable("empty_flat_activity_half")
        high_pass_delta = (
            candidate_high_pass.ravel()[flat_indices]
            - reference_high_pass.ravel()[flat_indices]
        )
        flat_rmse = math.sqrt(
            float(np.mean(high_pass_delta * high_pass_delta, dtype=np.float64))
        )

        candidate_energy = float(
            np.mean(
                candidate_gx * candidate_gx + candidate_gy * candidate_gy,
                dtype=np.float64,
            )
        )
        reference_energy = float(
            np.mean(
                reference_gx * reference_gx + reference_gy * reference_gy,
                dtype=np.float64,
            )
        )
        if reference_energy == 0.0 or not math.isfinite(reference_energy):
            raise MetricUnavailable("zero_reference_gradient_energy")
        energy_ratio = candidate_energy / reference_energy
        haar_cosine, haar_gain = _haar_values(
            candidate_luma, reference_luma, reference_magnitude
        )

        eligibility = {
            "luma_std": _minimum_metric(
                float(np.std(reference_luma, ddof=0, dtype=np.float64)),
                REFERENCE_LUMA_STD_MIN,
            ),
            "sobel_active_fraction": _minimum_metric(
                float(
                    np.mean(
                        reference_magnitude > REFERENCE_EDGE_ACTIVE_MAGNITUDE,
                        dtype=np.float64,
                    )
                ),
                REFERENCE_EDGE_ACTIVE_FRACTION_MIN,
            ),
            "sobel_active_magnitude_exclusive": REFERENCE_EDGE_ACTIVE_MAGNITUDE,
        }
        eligibility["pass"] = bool(
            eligibility["luma_std"]["pass"]
            and eligibility["sobel_active_fraction"]["pass"]
        )
        metrics = {
            "global_rgb_agreement": _minimum_metric(
                global_score, GLOBAL_RGB_AGREEMENT_MIN
            ),
            "worst_regional_rgb_agreement": _minimum_metric(
                regional_score, REGIONAL_RGB_AGREEMENT_MIN
            ),
            "worst_microtile_rgb_agreement": _minimum_metric(
                micro_score, MICROTILE_RGB_AGREEMENT_MIN
            ),
            "activity_strata_rgb_agreement": activity_metrics,
            "gaussian_luma_ssim": _minimum_metric(ssim, SSIM_MIN),
            "regional_ssim_p5": _minimum_metric(ssim_p5, SSIM_REGIONAL_P5_MIN),
            "sobel_gms_mean": _minimum_metric(gms_mean, GMS_MEAN_MIN),
            "sobel_gmsd": _maximum_metric(gmsd, GMSD_MAX),
            "flat_high_pass_rmse": _maximum_metric(
                flat_rmse, FLAT_HIGH_PASS_RMSE_MAX
            ),
            "sobel_gradient_energy_ratio": _range_metric(
                energy_ratio,
                GRADIENT_ENERGY_RATIO_MIN,
                GRADIENT_ENERGY_RATIO_MAX,
            ),
            "haar_detail_cosine": _minimum_metric(
                haar_cosine, HAAR_DETAIL_COSINE_MIN
            ),
            "haar_detail_rms_gain": _range_metric(
                haar_gain, HAAR_DETAIL_GAIN_MIN, HAAR_DETAIL_GAIN_MAX
            ),
        }
        scalar_passes = [
            value["pass"]
            for key, value in metrics.items()
            if key != "activity_strata_rgb_agreement"
        ]
        activity_passes = [value["pass"] for value in activity_metrics.values()]
        regional = _agreement_grid(size)
        micro = _microtile_grid(size)
        return {
            "reference_eligibility": eligibility,
            "grid": {
                "regional": {
                    "columns": regional[0],
                    "rows": regional[1],
                    "count": regional[0] * regional[1],
                },
                "microtile": {
                    "columns": micro[0],
                    "rows": micro[1],
                    "count": micro[0] * micro[1],
                    "nominal_edge_pixels": MICROTILE_EDGE,
                },
            },
            "metrics": metrics,
            "unavailable_reasons": [],
            "pass": bool(
                eligibility["pass"]
                and all(scalar_passes)
                and all(activity_passes)
            ),
        }
    except (
        MetricUnavailable,
        FloatingPointError,
        MemoryError,
        OverflowError,
        ValueError,
        ZeroDivisionError,
    ) as exc:
        reason = exc.code if isinstance(exc, MetricUnavailable) else type(exc).__name__
        return _unavailable_matte(size, reason)


def _empty_result(
    target_size: tuple[int, int],
    candidate_sha256: str | None,
    candidate_bytes: int | None,
    reference_sha256: str | None,
    reference_bytes: int | None,
    errors: list[str],
) -> dict[str, Any]:
    runtime = runtime_identity()
    return {
        "kind": RESULT_KIND,
        "schema_version": SCHEMA_VERSION,
        "contract": CONTRACT_DESCRIPTOR,
        "contract_sha256": _sha256_bytes(_canonical_json(CONTRACT_DESCRIPTOR)),
        "runtime": runtime,
        "inputs": {
            "target_dimensions": [target_size[0], target_size[1]],
            "candidate": {
                "bytes": candidate_bytes,
                "sha256": candidate_sha256,
                "mode": None,
            },
            "reference": {
                "bytes": reference_bytes,
                "sha256": reference_sha256,
                "mode": None,
            },
        },
        "alpha_agreement": _minimum_metric(None, ALPHA_AGREEMENT_MIN),
        "mattes": {
            "black": _unavailable_matte(target_size, "input_rejected"),
            "white": _unavailable_matte(target_size, "input_rejected"),
        },
        "failures": ["input"] if errors else [],
        "errors": errors,
        "pass": False,
    }


def evaluate_png_bytes(
    candidate_data: bytes,
    reference_data: bytes,
    *,
    target_size: Sequence[int],
) -> dict[str, Any]:
    """Evaluate two in-memory PNG artifacts under the complete v3 contract."""

    try:
        size = _validate_target_size(target_size)
    except QualityInputError as exc:
        # A deterministic placeholder is necessary even when dimensions are bad.
        size = (1, 1)
        return _empty_result(
            size,
            _sha256_bytes(candidate_data),
            len(candidate_data),
            _sha256_bytes(reference_data),
            len(reference_data),
            [f"target:{exc.code}"],
        )
    errors: list[str] = []
    decoded: dict[str, tuple[np.ndarray, np.ndarray, str]] = {}
    for label, data in (("candidate", candidate_data), ("reference", reference_data)):
        try:
            decoded[label] = _decode_png(data, size)
        except QualityInputError as exc:
            errors.append(f"{label}:{exc.code}")
    if errors:
        return _empty_result(
            size,
            _sha256_bytes(candidate_data),
            len(candidate_data),
            _sha256_bytes(reference_data),
            len(reference_data),
            errors,
        )

    candidate_rgb, candidate_alpha, candidate_mode = decoded["candidate"]
    reference_rgb, reference_alpha, reference_mode = decoded["reference"]
    if not (
        np.isfinite(candidate_rgb).all()
        and np.isfinite(candidate_alpha).all()
        and np.isfinite(reference_rgb).all()
        and np.isfinite(reference_alpha).all()
    ):
        return _empty_result(
            size,
            _sha256_bytes(candidate_data),
            len(candidate_data),
            _sha256_bytes(reference_data),
            len(reference_data),
            ["decoded:nonfinite_values"],
        )
    alpha_metric = _minimum_metric(
        1.0
        - float(
            np.mean(
                np.abs(candidate_alpha - reference_alpha), dtype=np.float64
            )
        ),
        ALPHA_AGREEMENT_MIN,
    )
    mattes: dict[str, Any] = {}
    for label, matte in MATTE_VALUES:
        candidate_composite = (
            candidate_alpha[..., None] * candidate_rgb
            + (1.0 - candidate_alpha[..., None]) * matte
        )
        reference_composite = (
            reference_alpha[..., None] * reference_rgb
            + (1.0 - reference_alpha[..., None]) * matte
        )
        mattes[label] = _evaluate_matte(
            candidate_composite, reference_composite, size
        )
    failures: list[str] = []
    if not alpha_metric["pass"]:
        failures.append("alpha_agreement")
    for label, _matte in MATTE_VALUES:
        if not mattes[label]["pass"]:
            failures.append(f"matte:{label}")
    runtime = runtime_identity()
    return {
        "kind": RESULT_KIND,
        "schema_version": SCHEMA_VERSION,
        "contract": CONTRACT_DESCRIPTOR,
        "contract_sha256": _sha256_bytes(_canonical_json(CONTRACT_DESCRIPTOR)),
        "runtime": runtime,
        "inputs": {
            "target_dimensions": [size[0], size[1]],
            "candidate": {
                "bytes": len(candidate_data),
                "sha256": _sha256_bytes(candidate_data),
                "mode": candidate_mode,
            },
            "reference": {
                "bytes": len(reference_data),
                "sha256": _sha256_bytes(reference_data),
                "mode": reference_mode,
            },
        },
        "alpha_agreement": alpha_metric,
        "mattes": mattes,
        "failures": failures,
        "errors": [],
        "pass": bool(not failures),
    }


def evaluate_pngs(
    candidate_path: str | Path,
    reference_path: str | Path,
    *,
    target_size: Sequence[int],
) -> dict[str, Any]:
    """Evaluate strict PNG files and return a deterministic, fail-closed result."""

    size: tuple[int, int]
    try:
        size = _validate_target_size(target_size)
    except QualityInputError as exc:
        size = (1, 1)
        return _empty_result(size, None, None, None, None, [f"target:{exc.code}"])
    errors: list[str] = []
    values: dict[str, bytes] = {}
    for label, path in (("candidate", candidate_path), ("reference", reference_path)):
        try:
            values[label] = _read_png_bytes(path)
        except QualityInputError as exc:
            errors.append(f"{label}:{exc.code}")
    if errors:
        return _empty_result(
            size,
            _sha256_bytes(values["candidate"]) if "candidate" in values else None,
            len(values["candidate"]) if "candidate" in values else None,
            _sha256_bytes(values["reference"]) if "reference" in values else None,
            len(values["reference"]) if "reference" in values else None,
            errors,
        )
    return evaluate_png_bytes(
        values["candidate"], values["reference"], target_size=size
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate a candidate PNG against a high-SPP reference under quality v3."
    )
    parser.add_argument("candidate", help="candidate 8-bit RGB/RGBA PNG")
    parser.add_argument("reference", help="reference 8-bit RGB/RGBA PNG")
    parser.add_argument("--width", type=int, required=True, help="exact target width")
    parser.add_argument("--height", type=int, required=True, help="exact target height")
    parser.add_argument(
        "--pretty", action="store_true", help="indent the deterministic JSON output"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = evaluate_pngs(
        args.candidate,
        args.reference,
        target_size=(args.width, args.height),
    )
    if args.pretty:
        payload = json.dumps(
            result,
            sort_keys=True,
            indent=2,
            ensure_ascii=True,
            allow_nan=False,
        )
    else:
        payload = _canonical_json(result).decode("ascii")
    sys.stdout.write(payload + "\n")
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
