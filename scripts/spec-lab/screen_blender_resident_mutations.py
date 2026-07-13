#!/usr/bin/env python3
"""Screen which resident Cycles mutations trigger an expensive scene rebuild.

Run this file inside Blender, for example::

    Blender -b --python scripts/spec-lab/screen_blender_resident_mutations.py -- \
      --scene /absolute/scene.blend --output-dir /absolute/new-output-dir \
      --width 512 --height 214 --frame 2472 --samples 4 --device METAL

This is an experimental bottleneck-isolation harness, not a production-change
authorization.  After one broad production-style warmup, it renders A -> B -> A
for four broad oracle configuration schemes and five narrower mutation arms:

* reapply constant integrator/sampling/resolution settings without frame_set/tag;
* set only filepath, seed, and sample offset (samples are pre-set once);
* change only seed while sample offset remains zero;
* change only sample offset while seed remains fixed; and
* change only filepath as an A/A/A control.

Every narrow arm is compared with a broad oracle using exactly matching seed and
sample-offset configurations.  Exact hashes and changed-pixel diagnostics are
retained, but semantic equivalence deliberately tolerates the handful of pixel
code-value drifts observed on Metal.  Conversely, an independence arm's B must
be materially different from A.  Pure report evaluation and validation do not
import Blender and therefore have subprocess-free unit tests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import time
from typing import Any


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# Reuse the bounded PNG decoder and atomic artifact helpers from the adjacent
# invalidation screen. They are pure Python and import bpy only at their own
# runtime boundary.
from screen_blender_resident_invalidation import (  # noqa: E402
    ScreenError,
    _decode_png_rgb,
    _round9,
    _sha256_file,
    _write_json_atomic,
)


KIND = "cx_blender_resident_mutation_screen"
SCHEMA_VERSION = 1
REPORT_NAME = "resident-mutation-screen.json"
MAX_DIMENSION = 4096
MAX_PIXELS = 16_777_216
MAX_SAMPLES = 4096
RENDER_ORDER = ("a1", "b", "a2")
INTEGRATOR_KEYS = (
    "max_bounces",
    "diffuse_bounces",
    "glossy_bounces",
    "transmission_bounces",
)

# A few changed 8-bit pixels are permitted for Metal repeatability and
# broad-versus-narrow comparisons.  The B-independence threshold is deliberately
# much farther away: at least 1% of pixels and at least 0.255 mean code values
# per RGB channel across the whole image, with a >=2-code-value excursion.
SEMANTIC_AGREEMENT_MIN = 0.99999
SEMANTIC_CHANGED_PIXEL_FRACTION_MAX = 0.0001
MATERIAL_CHANGED_PIXEL_FRACTION_MIN = 0.01
MATERIAL_MEAN_ABS_CHANNEL_DIFFERENCE_MIN = 0.001
MATERIAL_MAX_ABS_CHANNEL_DIFFERENCE_MIN = 2

SCHEME_ORDER = ("combined", "seed_only", "offset_only", "filepath_control")
SCHEME_SPECS = {
    "combined": {"b_should_differ": True},
    "seed_only": {"b_should_differ": True},
    "offset_only": {"b_should_differ": True},
    "filepath_control": {"b_should_differ": False},
}

# ``constant_setters`` restores the production worker's fixed integrator,
# non-adaptive sampling, denoising-off, sample count, and resolution settings.
# All arms set filepath because Blender needs a distinct immutable artifact.
ARM_SPECS = {
    "oracle_combined": {
        "scheme": "combined",
        "oracle": True,
        "frame_set": True,
        "constant_setters": True,
        "variable_setters": ("seed", "sample_offset"),
        "full_tag": True,
    },
    "all_constants_no_frame_or_tag": {
        "scheme": "combined",
        "oracle": False,
        "frame_set": False,
        "constant_setters": True,
        "variable_setters": ("seed", "sample_offset"),
        "full_tag": False,
    },
    "minimal_seed_and_offset": {
        "scheme": "combined",
        "oracle": False,
        "frame_set": False,
        "constant_setters": False,
        "variable_setters": ("seed", "sample_offset"),
        "full_tag": False,
    },
    "oracle_seed_only": {
        "scheme": "seed_only",
        "oracle": True,
        "frame_set": True,
        "constant_setters": True,
        "variable_setters": ("seed", "sample_offset"),
        "full_tag": True,
    },
    "seed_only": {
        "scheme": "seed_only",
        "oracle": False,
        "frame_set": False,
        "constant_setters": False,
        "variable_setters": ("seed",),
        "full_tag": False,
    },
    "oracle_offset_only": {
        "scheme": "offset_only",
        "oracle": True,
        "frame_set": True,
        "constant_setters": True,
        "variable_setters": ("seed", "sample_offset"),
        "full_tag": True,
    },
    "offset_only": {
        "scheme": "offset_only",
        "oracle": False,
        "frame_set": False,
        "constant_setters": False,
        "variable_setters": ("sample_offset",),
        "full_tag": False,
    },
    "oracle_filepath_control": {
        "scheme": "filepath_control",
        "oracle": True,
        "frame_set": True,
        "constant_setters": True,
        "variable_setters": ("seed", "sample_offset"),
        "full_tag": True,
    },
    "filepath_only_control": {
        "scheme": "filepath_control",
        "oracle": False,
        "frame_set": False,
        "constant_setters": False,
        "variable_setters": (),
        "full_tag": False,
    },
}
ARM_ORDER = tuple(ARM_SPECS)
ORACLE_BY_SCHEME = {
    spec["scheme"]: name for name, spec in ARM_SPECS.items() if spec["oracle"]
}
TIMING_KEYS = (
    "constant_setters",
    "explicit_tag",
    "frame_set",
    "render_operator",
    "total",
    "variable_setters",
    "view_layer_update",
)


def _configs(
    *, samples: int, seed_a: int, seed_b: int
) -> dict[str, dict[str, dict[str, Any]]]:
    """Return the closed A/B configuration set for every oracle scheme."""
    raw = {
        "combined": ((seed_a, 0), (seed_b, samples)),
        "seed_only": ((seed_a, 0), (seed_b, 0)),
        "offset_only": ((seed_a, 0), (seed_a, samples)),
        "filepath_control": ((seed_a, 0), (seed_a, 0)),
    }
    output: dict[str, dict[str, dict[str, Any]]] = {}
    for scheme in SCHEME_ORDER:
        a_values, b_values = raw[scheme]
        output[scheme] = {}
        for config_name, values in (("a", a_values), ("b", b_values)):
            seed, offset = values
            output[scheme][config_name] = {
                "sample_offset": offset,
                "sample_range": [offset, offset + samples],
                "samples": samples,
                "seed": seed,
            }
    return output


def _difference_stats(left: bytes, right: bytes) -> dict[str, Any]:
    """Return exact RGB difference counts plus bounded semantic classifications."""
    if not left or len(left) != len(right) or len(left) % 3:
        raise ScreenError("decoded RGB buffers are incompatible")
    sum_abs = 0
    changed_pixels = 0
    changed_channels = 0
    max_abs = 0
    channel_sums = [0, 0, 0]
    channel_changed = [0, 0, 0]
    channel_max = [0, 0, 0]
    for index in range(0, len(left), 3):
        pixel_changed = False
        for channel in range(3):
            difference = abs(left[index + channel] - right[index + channel])
            sum_abs += difference
            channel_sums[channel] += difference
            if difference:
                changed_channels += 1
                channel_changed[channel] += 1
                pixel_changed = True
            if difference > max_abs:
                max_abs = difference
            if difference > channel_max[channel]:
                channel_max[channel] = difference
        if pixel_changed:
            changed_pixels += 1
    pixels = len(left) // 3
    mean_abs_normalized = sum_abs / (len(left) * 255.0)
    agreement = _round9(1.0 - mean_abs_normalized)
    changed_fraction = _round9(changed_pixels / pixels)
    mean_abs_normalized = _round9(mean_abs_normalized)
    semantic = (
        agreement >= SEMANTIC_AGREEMENT_MIN
        and changed_fraction <= SEMANTIC_CHANGED_PIXEL_FRACTION_MAX
    )
    material = (
        changed_fraction >= MATERIAL_CHANGED_PIXEL_FRACTION_MIN
        and mean_abs_normalized >= MATERIAL_MEAN_ABS_CHANNEL_DIFFERENCE_MIN
        and max_abs >= MATERIAL_MAX_ABS_CHANNEL_DIFFERENCE_MIN
    )
    return {
        "agreement": agreement,
        "changed_channel_counts_rgb": channel_changed,
        "changed_channels": changed_channels,
        "changed_pixel_fraction": changed_fraction,
        "changed_pixels": changed_pixels,
        "materially_different": material,
        "max_abs_channel_difference": max_abs,
        "max_abs_difference_rgb": channel_max,
        "mean_abs_channel_difference_normalized": mean_abs_normalized,
        "semantic_equivalent": semantic,
        "sum_abs_channel_difference": sum_abs,
        "sum_abs_difference_rgb": channel_sums,
    }


def _comparison(
    left: dict[str, Any], right: dict[str, Any], stats: dict[str, Any]
) -> dict[str, Any]:
    return {
        **stats,
        "left": left["id"],
        "pixel_identical": (
            left["decoded_rgb_sha256"] == right["decoded_rgb_sha256"]
        ),
        "right": right["id"],
    }


def _records_by_label(arm: dict[str, Any]) -> dict[str, dict[str, Any]]:
    records = arm.get("renders")
    if not isinstance(records, list):
        return {}
    return {
        record["label"]: record
        for record in records
        if isinstance(record, dict) and isinstance(record.get("label"), str)
    }


def _arm_timing_summary(arm: dict[str, Any]) -> dict[str, Any]:
    records = arm.get("renders")
    if not isinstance(records, list) or len(records) != len(RENDER_ORDER):
        return {
            "complete": False,
            "median_render_operator_s": None,
            "median_total_s": None,
            "sequence_total_s": None,
        }
    totals = [float(record["timings_s"]["total"]) for record in records]
    renders = [
        float(record["timings_s"]["render_operator"]) for record in records
    ]
    return {
        "complete": True,
        "median_render_operator_s": _round9(statistics.median(renders)),
        "median_total_s": _round9(statistics.median(totals)),
        "sequence_total_s": _round9(sum(totals)),
    }


def summarize_timings(arms: dict[str, Any]) -> dict[str, Any]:
    """Summarize render timings and narrow-arm speedups against their oracle."""
    if set(arms) != set(ARM_ORDER):
        raise ScreenError("arm set is not closed")
    summaries = {name: _arm_timing_summary(arms[name]) for name in ARM_ORDER}
    for name in ARM_ORDER:
        spec = ARM_SPECS[name]
        summary = summaries[name]
        oracle_name = ORACLE_BY_SCHEME[spec["scheme"]]
        oracle = summaries[oracle_name]
        speedup: float | None = None
        if (
            not spec["oracle"]
            and summary["complete"]
            and oracle["complete"]
            and summary["median_total_s"] > 0
        ):
            speedup = _round9(
                oracle["median_total_s"] / summary["median_total_s"]
            )
        summary["median_total_speedup_vs_oracle"] = speedup
        summary["oracle_arm"] = oracle_name
    return summaries


def evaluate_mutations(arms: dict[str, Any]) -> dict[str, Any]:
    """Evaluate semantic repeatability, independence, and oracle agreement."""
    if set(arms) != set(ARM_ORDER):
        raise ScreenError("arm set is not closed")
    decisions: dict[str, Any] = {}
    usable_oracles: dict[str, bool] = {}

    for scheme in SCHEME_ORDER:
        name = ORACLE_BY_SCHEME[scheme]
        arm = arms[name]
        records = _records_by_label(arm)
        reasons: list[str] = []
        if arm.get("supported") is not True or arm.get("error") is not None:
            reasons.append("oracle_failed")
        if set(records) != set(RENDER_ORDER):
            reasons.append("render_set_incomplete")
        else:
            pairs = arm.get("pair_comparisons")
            if not isinstance(pairs, dict):
                reasons.append("pair_comparisons_missing")
            else:
                repeat = pairs.get("a1_vs_a2")
                if not isinstance(repeat, dict) or repeat.get(
                    "semantic_equivalent"
                ) is not True:
                    reasons.append("a_repeat_not_semantically_equivalent")
                b_pair = pairs.get("a1_vs_b")
                if SCHEME_SPECS[scheme]["b_should_differ"]:
                    if not isinstance(b_pair, dict) or b_pair.get(
                        "materially_different"
                    ) is not True:
                        reasons.append("b_not_materially_different")
                elif not isinstance(b_pair, dict) or b_pair.get(
                    "semantic_equivalent"
                ) is not True:
                    reasons.append("filepath_control_b_not_equivalent")
        reasons = list(dict.fromkeys(reasons))
        usable = not reasons
        usable_oracles[scheme] = usable
        decisions[name] = {
            "oracle_usable": usable,
            "requirements_met": usable,
            "reasons": reasons,
        }

    passing_narrow: list[str] = []
    for name in ARM_ORDER:
        spec = ARM_SPECS[name]
        if spec["oracle"]:
            continue
        arm = arms[name]
        scheme = spec["scheme"]
        records = _records_by_label(arm)
        reasons: list[str] = []
        if not usable_oracles[scheme]:
            reasons.append("corresponding_oracle_invalid")
        if arm.get("supported") is not True or arm.get("error") is not None:
            reasons.append("arm_failed")
        if set(records) != set(RENDER_ORDER):
            reasons.append("render_set_incomplete")
        else:
            pairs = arm.get("pair_comparisons")
            if not isinstance(pairs, dict):
                reasons.append("pair_comparisons_missing")
            else:
                repeat = pairs.get("a1_vs_a2")
                if not isinstance(repeat, dict) or repeat.get(
                    "semantic_equivalent"
                ) is not True:
                    reasons.append("a_repeat_not_semantically_equivalent")
                b_pair = pairs.get("a1_vs_b")
                if SCHEME_SPECS[scheme]["b_should_differ"]:
                    if not isinstance(b_pair, dict) or b_pair.get(
                        "materially_different"
                    ) is not True:
                        reasons.append("b_not_materially_different")
                elif not isinstance(b_pair, dict) or b_pair.get(
                    "semantic_equivalent"
                ) is not True:
                    reasons.append("filepath_control_b_not_equivalent")
            oracle_comparisons = arm.get("oracle_comparisons")
            if (
                not isinstance(oracle_comparisons, dict)
                or set(oracle_comparisons) != set(RENDER_ORDER)
            ):
                reasons.append("oracle_comparisons_incomplete")
            else:
                for label in RENDER_ORDER:
                    comparison = oracle_comparisons[label]
                    if not isinstance(comparison, dict) or comparison.get(
                        "semantic_equivalent"
                    ) is not True:
                        reasons.append(f"{label}_differs_from_broad_oracle")
        reasons = list(dict.fromkeys(reasons))
        passed = not reasons
        decisions[name] = {
            "oracle_arm": ORACLE_BY_SCHEME[scheme],
            "requirements_met": passed,
            "reasons": reasons,
        }
        if passed:
            passing_narrow.append(name)

    timing = summarize_timings(arms)
    fastest: str | None = None
    eligible = [
        name
        for name in passing_narrow
        if timing[name]["median_total_s"] is not None
    ]
    if eligible:
        fastest = min(eligible, key=lambda name: timing[name]["median_total_s"])
    return {
        "arms": decisions,
        "experimental_only": True,
        "fastest_requirements_met_narrow_arm": fastest,
        "passing_narrow_arms": passing_narrow,
        "production_change_authorized": False,
        "usable_oracles": usable_oracles,
    }


def _require_hash(value: Any, location: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise ScreenError(f"{location} is not a SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ScreenError(f"{location} is not hexadecimal") from exc


def _validate_difference(
    comparison: Any,
    left: dict[str, Any],
    right: dict[str, Any],
    location: str,
) -> None:
    expected_keys = {
        "agreement",
        "changed_channel_counts_rgb",
        "changed_channels",
        "changed_pixel_fraction",
        "changed_pixels",
        "left",
        "materially_different",
        "max_abs_channel_difference",
        "max_abs_difference_rgb",
        "mean_abs_channel_difference_normalized",
        "pixel_identical",
        "right",
        "semantic_equivalent",
        "sum_abs_channel_difference",
        "sum_abs_difference_rgb",
    }
    if not isinstance(comparison, dict) or set(comparison) != expected_keys:
        raise ScreenError(f"{location} shape mismatch")
    if comparison["left"] != left["id"] or comparison["right"] != right["id"]:
        raise ScreenError(f"{location} endpoint mismatch")
    finite_unit_fields = (
        "agreement",
        "changed_pixel_fraction",
        "mean_abs_channel_difference_normalized",
    )
    for field in finite_unit_fields:
        value = comparison[field]
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or not 0.0 <= float(value) <= 1.0
        ):
            raise ScreenError(f"{location}.{field} is invalid")
    for field in (
        "changed_channels",
        "changed_pixels",
        "max_abs_channel_difference",
        "sum_abs_channel_difference",
    ):
        value = comparison[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ScreenError(f"{location}.{field} is invalid")
    for field in (
        "changed_channel_counts_rgb",
        "max_abs_difference_rgb",
        "sum_abs_difference_rgb",
    ):
        values = comparison[field]
        if (
            not isinstance(values, list)
            or len(values) != 3
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in values
            )
        ):
            raise ScreenError(f"{location}.{field} is invalid")
    if comparison["max_abs_channel_difference"] > 255 or any(
        value > 255 for value in comparison["max_abs_difference_rgb"]
    ):
        raise ScreenError(f"{location}.max_abs difference exceeds 8-bit range")
    identical = left["decoded_rgb_sha256"] == right["decoded_rgb_sha256"]
    if comparison["pixel_identical"] is not identical:
        raise ScreenError(f"{location}.pixel_identical disagrees with hashes")
    if identical and (
        comparison["agreement"] != 1.0
        or comparison["changed_channels"] != 0
        or comparison["changed_pixels"] != 0
        or comparison["sum_abs_channel_difference"] != 0
    ):
        raise ScreenError(f"{location} exact hashes have nonzero difference")
    expected_semantic = (
        comparison["agreement"] >= SEMANTIC_AGREEMENT_MIN
        and comparison["changed_pixel_fraction"]
        <= SEMANTIC_CHANGED_PIXEL_FRACTION_MAX
    )
    expected_material = (
        comparison["changed_pixel_fraction"]
        >= MATERIAL_CHANGED_PIXEL_FRACTION_MIN
        and comparison["mean_abs_channel_difference_normalized"]
        >= MATERIAL_MEAN_ABS_CHANNEL_DIFFERENCE_MIN
        and comparison["max_abs_channel_difference"]
        >= MATERIAL_MAX_ABS_CHANNEL_DIFFERENCE_MIN
    )
    if comparison["semantic_equivalent"] is not expected_semantic:
        raise ScreenError(f"{location}.semantic_equivalent is inconsistent")
    if comparison["materially_different"] is not expected_material:
        raise ScreenError(f"{location}.materially_different is inconsistent")


def validate_report(report: dict[str, Any]) -> None:
    """Fail closed on report shape, decisive semantics, and non-finite timings."""
    required = {
        "arms",
        "blender",
        "configuration",
        "decision",
        "experimental_only",
        "host",
        "kind",
        "limitations",
        "scene",
        "schema_version",
        "timing_summaries",
        "warmup",
    }
    if not isinstance(report, dict) or set(report) != required:
        raise ScreenError("report shape is not closed")
    if report["schema_version"] != SCHEMA_VERSION or report["kind"] != KIND:
        raise ScreenError("report identity mismatch")
    if report["experimental_only"] is not True:
        raise ScreenError("screen must remain experimental-only")
    arms = report["arms"]
    if not isinstance(arms, dict) or set(arms) != set(ARM_ORDER):
        raise ScreenError("report arm set mismatch")

    for name in ARM_ORDER:
        arm = arms[name]
        spec = ARM_SPECS[name]
        expected_arm_keys = {
            "actions",
            "arm",
            "error",
            "oracle_arm",
            "oracle_comparisons",
            "pair_comparisons",
            "prearm_setters_s",
            "renders",
            "scheme",
            "supported",
        }
        if not isinstance(arm, dict) or set(arm) != expected_arm_keys:
            raise ScreenError(f"arms.{name} shape mismatch")
        if (
            arm["arm"] != name
            or arm["scheme"] != spec["scheme"]
            or arm["oracle_arm"] != ORACLE_BY_SCHEME[spec["scheme"]]
            or type(arm["supported"]) is not bool
        ):
            raise ScreenError(f"arms.{name} identity mismatch")
        prearm = arm["prearm_setters_s"]
        if (
            not isinstance(prearm, (int, float))
            or isinstance(prearm, bool)
            or not math.isfinite(float(prearm))
            or prearm < 0
        ):
            raise ScreenError(f"arms.{name}.prearm_setters_s is invalid")
        records = arm["renders"]
        if not isinstance(records, list):
            raise ScreenError(f"arms.{name}.renders must be a list")
        labels: list[str] = []
        by_label: dict[str, dict[str, Any]] = {}
        for record in records:
            expected_record_keys = {
                "bytes",
                "config",
                "decoded_rgb_sha256",
                "height",
                "id",
                "label",
                "path",
                "sample_offset",
                "sample_range",
                "samples",
                "seed",
                "sha256",
                "timings_s",
                "width",
            }
            if not isinstance(record, dict) or set(record) != expected_record_keys:
                raise ScreenError(f"arms.{name}.render shape mismatch")
            label = record["label"]
            labels.append(label)
            by_label[label] = record
            if record["id"] != f"{name}/{label}":
                raise ScreenError(f"arms.{name}.render id mismatch")
            _require_hash(record["sha256"], f"arms.{name}.render.sha256")
            _require_hash(
                record["decoded_rgb_sha256"],
                f"arms.{name}.render.decoded_rgb_sha256",
            )
            timings = record["timings_s"]
            if not isinstance(timings, dict) or set(timings) != set(TIMING_KEYS):
                raise ScreenError(f"arms.{name}.render timings shape mismatch")
            for key, value in timings.items():
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not math.isfinite(float(value))
                    or value < 0
                ):
                    raise ScreenError(
                        f"arms.{name}.render.timings_s.{key} is invalid"
                    )
            if timings["render_operator"] <= 0 or timings["total"] <= 0:
                raise ScreenError(f"arms.{name}.render render timing is not positive")
        if labels and labels != list(RENDER_ORDER[: len(labels)]):
            raise ScreenError(f"arms.{name}.render order mismatch")

        expected_pairs = {
            key: (by_label[left], by_label[right])
            for key, left, right in (
                ("a1_vs_a2", "a1", "a2"),
                ("a1_vs_b", "a1", "b"),
                ("b_vs_a2", "b", "a2"),
            )
            if left in by_label and right in by_label
        }
        if set(arm["pair_comparisons"]) != set(expected_pairs):
            raise ScreenError(f"arms.{name}.pair_comparisons shape mismatch")
        for key, endpoints in expected_pairs.items():
            _validate_difference(
                arm["pair_comparisons"][key],
                endpoints[0],
                endpoints[1],
                f"arms.{name}.pair_comparisons.{key}",
            )

    for name in ARM_ORDER:
        arm = arms[name]
        spec = ARM_SPECS[name]
        oracle_name = ORACLE_BY_SCHEME[spec["scheme"]]
        if spec["oracle"]:
            if arm["oracle_comparisons"] != {}:
                raise ScreenError(f"arms.{name} oracle must not compare with itself")
            continue
        by_label = _records_by_label(arm)
        oracle_by_label = _records_by_label(arms[oracle_name])
        expected_labels = (
            set(RENDER_ORDER)
            if set(by_label) == set(RENDER_ORDER)
            and set(oracle_by_label) == set(RENDER_ORDER)
            else set()
        )
        if set(arm["oracle_comparisons"]) != expected_labels:
            raise ScreenError(f"arms.{name}.oracle_comparisons shape mismatch")
        for label in expected_labels:
            _validate_difference(
                arm["oracle_comparisons"][label],
                by_label[label],
                oracle_by_label[label],
                f"arms.{name}.oracle_comparisons.{label}",
            )

    recomputed_timings = summarize_timings(arms)
    if report["timing_summaries"] != recomputed_timings:
        raise ScreenError("stored timing summaries disagree with render records")
    recomputed_decision = evaluate_mutations(arms)
    if report["decision"] != recomputed_decision:
        raise ScreenError("stored decision disagrees with semantic comparisons")


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


def _run_blender(args: argparse.Namespace) -> dict[str, Any]:
    try:
        import bpy  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - Blender runtime boundary
        raise ScreenError("this experimental harness must run inside Blender") from exc

    source = args.scene.expanduser().resolve(strict=True)
    if not source.is_file() or source.is_symlink():
        raise ScreenError("scene must be a regular non-symlink file")
    source_sha_before, source_bytes = _sha256_file(source)
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
    for layer in scene.view_layers:
        if hasattr(layer.cycles, "use_denoising"):
            layer.cycles.use_denoising = False
    actual_device, enabled_devices = _configure_device(bpy, scene, args.device)
    fixed_sampling = {
        "adaptive_min_samples": int(scene.cycles.adaptive_min_samples),
        "adaptive_threshold": float(scene.cycles.adaptive_threshold),
        "use_adaptive_sampling": False,
        "use_light_tree": bool(scene.cycles.use_light_tree),
    }
    scene.frame_set(args.frame)

    seed_a = int(source_sha_before[:8], 16) & 0x7FFFFFFF
    seed_b = (seed_a ^ 0x5A5A5A5A) & 0x7FFFFFFF
    configs = _configs(samples=args.samples, seed_a=seed_a, seed_b=seed_b)
    decoded_cache: dict[tuple[str, str], bytes] = {}

    def apply_constants() -> None:
        for key, value in native_integrator.items():
            setattr(scene.cycles, key, value)
        scene.cycles.use_light_tree = fixed_sampling["use_light_tree"]
        scene.cycles.use_adaptive_sampling = False
        scene.cycles.adaptive_min_samples = fixed_sampling["adaptive_min_samples"]
        scene.cycles.adaptive_threshold = fixed_sampling["adaptive_threshold"]
        scene.cycles.use_denoising = False
        for layer in scene.view_layers:
            if hasattr(layer.cycles, "use_denoising"):
                layer.cycles.use_denoising = False
        scene.cycles.samples = args.samples
        scene.render.resolution_x = args.width
        scene.render.resolution_y = args.height
        scene.render.resolution_percentage = 100

    def preset_arm(name: str) -> float:
        """Set values excluded from the arm's per-render mutation list once."""
        spec = ARM_SPECS[name]
        scheme = spec["scheme"]
        config_a = configs[scheme]["a"]
        started = time.perf_counter()
        # Samples are intentionally pre-set once for every minimal arm.
        scene.cycles.samples = args.samples
        if "seed" not in spec["variable_setters"]:
            scene.cycles.seed = config_a["seed"]
        if "sample_offset" not in spec["variable_setters"]:
            scene.cycles.sample_offset = config_a["sample_offset"]
        if not spec["constant_setters"]:
            # These are pre-set, not per-render, so the mutation under study is
            # exactly the arm's documented variable setter list.
            apply_constants()
        return _round9(time.perf_counter() - started)

    def render_once(
        arm_name: str, label: str, config_name: str
    ) -> dict[str, Any]:
        spec = ARM_SPECS[arm_name]
        config = configs[spec["scheme"]][config_name]
        destination = output_dir / f"{arm_name}-{label}.png"
        if destination.exists():
            raise ScreenError("render destination unexpectedly exists")
        timings = {key: 0.0 for key in TIMING_KEYS}
        total_started = time.perf_counter()

        started = time.perf_counter()
        if spec["frame_set"]:
            scene.frame_set(args.frame)
        elif int(scene.frame_current) != args.frame:
            raise ScreenError("no-frame-set arm observed an unexpected current frame")
        timings["frame_set"] = time.perf_counter() - started

        started = time.perf_counter()
        if spec["constant_setters"]:
            apply_constants()
        timings["constant_setters"] = time.perf_counter() - started

        started = time.perf_counter()
        scene.render.filepath = str(destination)
        if "seed" in spec["variable_setters"]:
            scene.cycles.seed = config["seed"]
        if "sample_offset" in spec["variable_setters"]:
            scene.cycles.sample_offset = config["sample_offset"]
        timings["variable_setters"] = time.perf_counter() - started

        started = time.perf_counter()
        if spec["full_tag"]:
            scene.update_tag()
        timings["explicit_tag"] = time.perf_counter() - started

        started = time.perf_counter()
        bpy.context.view_layer.update()
        timings["view_layer_update"] = time.perf_counter() - started

        started = time.perf_counter()
        bpy.ops.render.render(write_still=True)
        timings["render_operator"] = time.perf_counter() - started
        timings["total"] = time.perf_counter() - total_started
        timings = {key: _round9(value) for key, value in timings.items()}

        digest, byte_count = _sha256_file(destination)
        rgb = _decode_png_rgb(
            destination,
            expected_width=args.width,
            expected_height=args.height,
        )
        decoded_cache[(arm_name, label)] = rgb
        return {
            "bytes": byte_count,
            "config": config_name,
            "decoded_rgb_sha256": hashlib.sha256(rgb).hexdigest(),
            "height": args.height,
            "id": f"{arm_name}/{label}",
            "label": label,
            "path": destination.name,
            "sample_offset": config["sample_offset"],
            "sample_range": config["sample_range"],
            "samples": config["samples"],
            "seed": config["seed"],
            "sha256": digest,
            "timings_s": timings,
            "width": args.width,
        }

    # The single warmup is deliberately broad and production-like.  It is not
    # reused as an oracle measurement.
    apply_constants()
    scene.cycles.seed = configs["combined"]["a"]["seed"]
    scene.cycles.sample_offset = configs["combined"]["a"]["sample_offset"]
    warmup_path = output_dir / "broad-warmup.png"
    scene.render.filepath = str(warmup_path)
    warmup_started = time.perf_counter()
    scene.frame_set(args.frame)
    scene.update_tag()
    bpy.context.view_layer.update()
    bpy.ops.render.render(write_still=True)
    warmup_wall = _round9(time.perf_counter() - warmup_started)
    warmup_sha, warmup_bytes = _sha256_file(warmup_path)
    warmup_rgb = _decode_png_rgb(
        warmup_path, expected_width=args.width, expected_height=args.height
    )
    warmup = {
        "bytes": warmup_bytes,
        "decoded_rgb_sha256": hashlib.sha256(warmup_rgb).hexdigest(),
        "path": warmup_path.name,
        "sha256": warmup_sha,
        "wall_s": warmup_wall,
    }

    arms: dict[str, Any] = {}
    for name in ARM_ORDER:
        spec = ARM_SPECS[name]
        records: list[dict[str, Any]] = []
        error: str | None = None
        supported = True
        prearm_s = preset_arm(name)
        try:
            for label, config_name in (("a1", "a"), ("b", "b"), ("a2", "a")):
                records.append(render_once(name, label, config_name))
        except BaseException as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
        by_label = {record["label"]: record for record in records}
        pairs: dict[str, Any] = {}
        for left_label, right_label in (("a1", "a2"), ("a1", "b"), ("b", "a2")):
            if left_label in by_label and right_label in by_label:
                stats = _difference_stats(
                    decoded_cache[(name, left_label)],
                    decoded_cache[(name, right_label)],
                )
                pairs[f"{left_label}_vs_{right_label}"] = _comparison(
                    by_label[left_label], by_label[right_label], stats
                )
        arms[name] = {
            "actions": {
                "constant_setters_per_render": spec["constant_setters"],
                "filepath_per_render": True,
                "frame_set_per_render": spec["frame_set"],
                "full_scene_tag_per_render": spec["full_tag"],
                "variable_setters_per_render": list(spec["variable_setters"]),
                "view_layer_update_per_render": True,
            },
            "arm": name,
            "error": error,
            "oracle_arm": ORACLE_BY_SCHEME[spec["scheme"]],
            "oracle_comparisons": {},
            "pair_comparisons": pairs,
            "prearm_setters_s": prearm_s,
            "renders": records,
            "scheme": spec["scheme"],
            "supported": supported,
        }

    # Match every narrow label to the corresponding broad oracle label.  Their
    # seed and offset values come from the same scheme config table.
    for name in ARM_ORDER:
        spec = ARM_SPECS[name]
        if spec["oracle"]:
            continue
        oracle_name = ORACLE_BY_SCHEME[spec["scheme"]]
        by_label = _records_by_label(arms[name])
        oracle_by_label = _records_by_label(arms[oracle_name])
        if set(by_label) != set(RENDER_ORDER) or set(oracle_by_label) != set(
            RENDER_ORDER
        ):
            continue
        for label in RENDER_ORDER:
            if (
                by_label[label]["seed"] != oracle_by_label[label]["seed"]
                or by_label[label]["sample_offset"]
                != oracle_by_label[label]["sample_offset"]
            ):
                raise ScreenError("narrow/oracle seed-offset config mismatch")
            stats = _difference_stats(
                decoded_cache[(name, label)],
                decoded_cache[(oracle_name, label)],
            )
            arms[name]["oracle_comparisons"][label] = _comparison(
                by_label[label], oracle_by_label[label], stats
            )

    source_sha_after, source_bytes_after = _sha256_file(source)
    if source_sha_after != source_sha_before or source_bytes_after != source_bytes:
        raise ScreenError("source scene changed during the read-only screen")
    build_hash = bpy.app.build_hash
    if isinstance(build_hash, bytes):
        build_hash = build_hash.decode("ascii", errors="replace")
    timing_summaries = summarize_timings(arms)
    decision = evaluate_mutations(arms)
    report = {
        "arms": arms,
        "blender": {
            "build_hash": str(build_hash),
            "version": str(bpy.app.version_string),
        },
        "configuration": {
            "arm_execution_order": list(ARM_ORDER),
            "configs": configs,
            "device": actual_device,
            "enabled_device_names": enabled_devices,
            "fixed_sampling": fixed_sampling,
            "frame": args.frame,
            "height": args.height,
            "native_integrator": native_integrator,
            "persistent_data": True,
            "png_compression": 0,
            "render_sequence_per_arm": list(RENDER_ORDER),
            "samples": args.samples,
            "thresholds": {
                "material_changed_pixel_fraction_min": (
                    MATERIAL_CHANGED_PIXEL_FRACTION_MIN
                ),
                "material_max_abs_channel_difference_min": (
                    MATERIAL_MAX_ABS_CHANNEL_DIFFERENCE_MIN
                ),
                "material_mean_abs_channel_difference_normalized_min": (
                    MATERIAL_MEAN_ABS_CHANNEL_DIFFERENCE_MIN
                ),
                "semantic_agreement_min": SEMANTIC_AGREEMENT_MIN,
                "semantic_changed_pixel_fraction_max": (
                    SEMANTIC_CHANGED_PIXEL_FRACTION_MAX
                ),
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
            "one local scene/frame/host trial cannot authorize a backend mutation change",
            "all narrow arms retain view_layer.update(), so this screen does not isolate its removal",
            "performance ordering can depend on arm history despite the broad warmup",
            "semantic RGB agreement does not establish animated, volume, compositor, or motion-blur safety",
        ],
        "scene": {
            "bytes": source_bytes,
            "path": str(source),
            "sha256": source_sha_before,
        },
        "schema_version": SCHEMA_VERSION,
        "timing_summaries": timing_summaries,
        "warmup": warmup,
    }
    validate_report(report)
    _write_json_atomic(output_dir / REPORT_NAME, report)
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
        print(
            json.dumps(
                {
                    "error": f"{type(exc).__name__}: {exc}"[:1000],
                    "kind": KIND,
                    "ok": False,
                    "schema_version": SCHEMA_VERSION,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )
        return 2
    print(
        json.dumps(
            {
                "decision": report["decision"],
                "kind": KIND,
                "ok": True,
                "report": REPORT_NAME,
                "schema_version": SCHEMA_VERSION,
                "timing_summaries": report["timing_summaries"],
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
