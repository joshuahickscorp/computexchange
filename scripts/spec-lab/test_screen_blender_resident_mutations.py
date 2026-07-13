#!/usr/bin/env python3
"""Subprocess-free tests for the resident Cycles mutation screen."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import sys
import unittest


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import screen_blender_resident_mutations as screen  # noqa: E402


PIXELS = 12_000
RGB_A1 = bytes(PIXELS * 3)
RGB_A2_DRIFT = bytes([1]) + bytes(PIXELS * 3 - 1)
RGB_B = bytes([20]) * (PIXELS * 3)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _record(
    arm: str,
    label: str,
    rgb: bytes,
    *,
    total_s: float,
) -> dict[str, object]:
    scheme = screen.ARM_SPECS[arm]["scheme"]
    config_name = "b" if label == "b" else "a"
    configs = screen._configs(samples=4, seed_a=101, seed_b=202)
    config = configs[scheme][config_name]
    timings = {key: 0.001 for key in screen.TIMING_KEYS}
    timings["render_operator"] = total_s - 0.01
    timings["total"] = total_s
    return {
        "bytes": 100,
        "config": config_name,
        "decoded_rgb_sha256": _digest(rgb),
        "height": 100,
        "id": f"{arm}/{label}",
        "label": label,
        "path": f"{arm}-{label}.png",
        "sample_offset": config["sample_offset"],
        "sample_range": config["sample_range"],
        "samples": config["samples"],
        "seed": config["seed"],
        "sha256": hashlib.sha256(f"artifact:{arm}:{label}".encode()).hexdigest(),
        "timings_s": timings,
        "width": 120,
    }


def _compare(
    left: dict[str, object],
    right: dict[str, object],
    left_rgb: bytes,
    right_rgb: bytes,
) -> dict[str, object]:
    return screen._comparison(
        left,
        right,
        screen._difference_stats(left_rgb, right_rgb),
    )


def _make_arm(name: str, *, total_s: float) -> tuple[dict[str, object], dict[str, bytes]]:
    scheme = screen.ARM_SPECS[name]["scheme"]
    b_rgb = RGB_B if screen.SCHEME_SPECS[scheme]["b_should_differ"] else RGB_A1
    buffers = {"a1": RGB_A1, "b": b_rgb, "a2": RGB_A2_DRIFT}
    renders = [
        _record(name, label, buffers[label], total_s=total_s)
        for label in screen.RENDER_ORDER
    ]
    by_label = {record["label"]: record for record in renders}
    pairs = {
        "a1_vs_a2": _compare(
            by_label["a1"], by_label["a2"], buffers["a1"], buffers["a2"]
        ),
        "a1_vs_b": _compare(
            by_label["a1"], by_label["b"], buffers["a1"], buffers["b"]
        ),
        "b_vs_a2": _compare(
            by_label["b"], by_label["a2"], buffers["b"], buffers["a2"]
        ),
    }
    spec = screen.ARM_SPECS[name]
    arm = {
        "actions": {
            "constant_setters_per_render": spec["constant_setters"],
            "filepath_per_render": True,
            "frame_set_per_render": spec["frame_set"],
            "full_scene_tag_per_render": spec["full_tag"],
            "variable_setters_per_render": list(spec["variable_setters"]),
            "view_layer_update_per_render": True,
        },
        "arm": name,
        "error": None,
        "oracle_arm": screen.ORACLE_BY_SCHEME[scheme],
        "oracle_comparisons": {},
        "pair_comparisons": pairs,
        "prearm_setters_s": 0.001,
        "renders": renders,
        "scheme": scheme,
        "supported": True,
    }
    return arm, buffers


def fixture_arms() -> dict[str, object]:
    total_by_arm = {
        "oracle_combined": 10.0,
        "all_constants_no_frame_or_tag": 3.0,
        "minimal_seed_and_offset": 2.0,
        "oracle_seed_only": 10.0,
        "seed_only": 1.5,
        "oracle_offset_only": 10.0,
        "offset_only": 1.0,
        "oracle_filepath_control": 10.0,
        "filepath_only_control": 0.5,
    }
    arms: dict[str, object] = {}
    buffers: dict[str, dict[str, bytes]] = {}
    for name in screen.ARM_ORDER:
        arms[name], buffers[name] = _make_arm(name, total_s=total_by_arm[name])
    for name in screen.ARM_ORDER:
        spec = screen.ARM_SPECS[name]
        if spec["oracle"]:
            continue
        oracle_name = screen.ORACLE_BY_SCHEME[spec["scheme"]]
        current = {record["label"]: record for record in arms[name]["renders"]}
        oracle = {
            record["label"]: record for record in arms[oracle_name]["renders"]
        }
        arms[name]["oracle_comparisons"] = {
            label: _compare(
                current[label],
                oracle[label],
                buffers[name][label],
                buffers[oracle_name][label],
            )
            for label in screen.RENDER_ORDER
        }
    return arms


def fixture_report() -> dict[str, object]:
    arms = fixture_arms()
    return {
        "arms": arms,
        "blender": {},
        "configuration": {},
        "decision": screen.evaluate_mutations(arms),
        "experimental_only": True,
        "host": {},
        "kind": screen.KIND,
        "limitations": [],
        "scene": {},
        "schema_version": screen.SCHEMA_VERSION,
        "timing_summaries": screen.summarize_timings(arms),
        "warmup": {},
    }


class ResidentMutationScreenTest(unittest.TestCase):
    def test_configuration_schemes_match_declared_mutations(self) -> None:
        configs = screen._configs(samples=4, seed_a=101, seed_b=202)
        self.assertEqual(configs["combined"]["b"]["sample_range"], [4, 8])
        self.assertNotEqual(
            configs["combined"]["a"]["seed"], configs["combined"]["b"]["seed"]
        )
        self.assertEqual(
            configs["seed_only"]["a"]["sample_offset"],
            configs["seed_only"]["b"]["sample_offset"],
        )
        self.assertEqual(
            configs["offset_only"]["a"]["seed"],
            configs["offset_only"]["b"]["seed"],
        )
        self.assertEqual(
            configs["filepath_control"]["a"],
            configs["filepath_control"]["b"],
        )

    def test_tiny_nonidentical_metal_drift_is_semantically_equivalent(self) -> None:
        stats = screen._difference_stats(RGB_A1, RGB_A2_DRIFT)
        self.assertTrue(stats["semantic_equivalent"])
        self.assertFalse(stats["materially_different"])
        self.assertEqual(stats["changed_pixels"], 1)
        self.assertEqual(stats["sum_abs_channel_difference"], 1)

    def test_all_required_arms_pass_and_timings_are_compared_to_oracle(self) -> None:
        arms = fixture_arms()
        decision = screen.evaluate_mutations(arms)
        self.assertEqual(set(decision["usable_oracles"]), set(screen.SCHEME_ORDER))
        self.assertTrue(all(decision["usable_oracles"].values()))
        self.assertEqual(
            decision["passing_narrow_arms"],
            [name for name in screen.ARM_ORDER if not screen.ARM_SPECS[name]["oracle"]],
        )
        self.assertFalse(decision["production_change_authorized"])
        summaries = screen.summarize_timings(arms)
        self.assertEqual(
            summaries["minimal_seed_and_offset"]["median_total_speedup_vs_oracle"],
            5.0,
        )

    def test_stale_seed_arm_fails_independence_and_oracle_match(self) -> None:
        arms = fixture_arms()
        arm = arms["seed_only"]
        records = {record["label"]: record for record in arm["renders"]}
        records["b"]["decoded_rgb_sha256"] = records["a1"]["decoded_rgb_sha256"]
        arm["pair_comparisons"]["a1_vs_b"] = _compare(
            records["a1"], records["b"], RGB_A1, RGB_A1
        )
        arm["pair_comparisons"]["b_vs_a2"] = _compare(
            records["b"], records["a2"], RGB_A1, RGB_A2_DRIFT
        )
        oracle_b = {
            record["label"]: record for record in arms["oracle_seed_only"]["renders"]
        }["b"]
        arm["oracle_comparisons"]["b"] = _compare(
            records["b"], oracle_b, RGB_A1, RGB_B
        )
        decision = screen.evaluate_mutations(arms)
        reasons = decision["arms"]["seed_only"]["reasons"]
        self.assertIn("b_not_materially_different", reasons)
        self.assertIn("b_differs_from_broad_oracle", reasons)

    def test_report_validation_recomputes_decision_and_rejects_nonfinite(self) -> None:
        report = fixture_report()
        screen.validate_report(report)

        changed = copy.deepcopy(report)
        changed["decision"]["passing_narrow_arms"] = []
        with self.assertRaisesRegex(screen.ScreenError, "stored decision"):
            screen.validate_report(changed)

        changed = copy.deepcopy(report)
        changed["arms"]["offset_only"]["renders"][0]["timings_s"][
            "render_operator"
        ] = float("nan")
        with self.assertRaisesRegex(screen.ScreenError, "render_operator"):
            screen.validate_report(changed)


if __name__ == "__main__":
    unittest.main()
