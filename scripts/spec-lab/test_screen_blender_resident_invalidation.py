#!/usr/bin/env python3
"""Subprocess-free tests for the resident Cycles invalidation screen."""

from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import screen_blender_resident_invalidation as screen  # noqa: E402


def record(label: str, digest: str, *, samples: int = 8) -> dict[str, object]:
    config = "b" if label == "b" else "a"
    offset = samples if config == "b" else 0
    return {
        "bytes": 100,
        "config": config,
        "decoded_rgb_sha256": digest * 64,
        "height": 64,
        "label": label,
        "path": f"{label}.png",
        "sample_offset": offset,
        "sample_range": [offset, offset + samples],
        "samples": samples,
        "seed": 2 if config == "b" else 1,
        "sha256": ("f" if digest != "f" else "e") * 64,
        "wall_s": 0.1,
        "width": 64,
    }


def comparison(left: dict[str, object], right: dict[str, object]) -> dict[str, object]:
    identical = left["decoded_rgb_sha256"] == right["decoded_rgb_sha256"]
    return {
        "agreement": 1.0 if identical else 0.9,
        "changed_pixel_fraction": 0.0 if identical else 0.01,
        "changed_pixels": 0 if identical else 41,
        "left": left["label"],
        "max_abs_channel_difference": 0 if identical else 10,
        "pixel_identical": identical,
        "right": right["label"],
        "semantic_equivalent": identical,
    }


def mode(name: str, a: str, b: str, a2: str) -> dict[str, object]:
    renders = [record("a1", a), record("b", b), record("a2", a2)]
    return {
        "error": None,
        "frame_action": "test",
        "frame_set_per_render": screen.MODE_SPECS[name]["frame_set_per_render"],
        "mode": name,
        "oracle_agreements": {},
        "pair_agreements": {
            "a1_vs_a2": comparison(renders[0], renders[2]),
            "a1_vs_b": comparison(renders[0], renders[1]),
            "b_vs_a2": comparison(renders[1], renders[2]),
        },
        "renders": renders,
        "supported": True,
        "tag_action": name,
    }


def attach_oracles(modes: dict[str, object]) -> None:
    full = {row["label"]: row for row in modes["full"]["renders"]}
    for row in modes.values():
        current = {render["label"]: render for render in row["renders"]}
        row["oracle_agreements"] = {
            label: comparison(current[label], full[label]) for label in screen.RENDER_ORDER
        }


def fixture_modes() -> dict[str, object]:
    modes = {
        name: mode(name, "a", "b", "a") for name in screen.MODE_ORDER
    }
    attach_oracles(modes)
    return modes


class ResidentInvalidationDecisionTest(unittest.TestCase):
    def test_valid_modes_match_full_oracle_and_stale_mode_fails(self) -> None:
        modes = fixture_modes()
        modes["time"] = mode("time", "a", "a", "a")
        attach_oracles(modes)
        decision = screen.evaluate_invalidation(modes)
        self.assertTrue(decision["full_anchor_valid"])
        self.assertTrue(decision["modes"]["none"]["valid_narrower_mode"])
        self.assertFalse(decision["modes"]["time"]["valid_narrower_mode"])
        self.assertIn(
            "b_differs_from_full_oracle", decision["modes"]["time"]["reasons"]
        )
        self.assertIn(
            "disjoint_range_did_not_change_pixels",
            decision["modes"]["time"]["reasons"],
        )
        self.assertEqual(
            decision["recommended_mode_for_followup_only"], "none_no_frame_set"
        )

    def test_invalid_full_anchor_blocks_every_narrower_mode(self) -> None:
        modes = fixture_modes()
        modes["full"] = mode("full", "a", "a", "a")
        decision = screen.evaluate_invalidation(modes)
        self.assertFalse(decision["full_anchor_valid"])
        self.assertEqual(decision["valid_narrower_modes"], [])
        self.assertEqual(decision["recommended_mode_for_followup_only"], "full")
        self.assertIn(
            "full_disjoint_range_did_not_change_pixels",
            decision["modes"]["full"]["reasons"],
        )

    def test_one_pixel_hash_drift_does_not_prove_independent_sampling(self) -> None:
        modes = fixture_modes()
        full = modes["full"]
        comparison_row = full["pair_agreements"]["a1_vs_b"]
        comparison_row.update(
            {
                "agreement": 0.9999999,
                "changed_pixel_fraction": 0.00001,
                "changed_pixels": 1,
                "max_abs_channel_difference": 1,
                "pixel_identical": False,
                "semantic_equivalent": True,
            }
        )
        decision = screen.evaluate_invalidation(modes)
        self.assertFalse(decision["full_anchor_valid"])
        self.assertIn(
            "full_disjoint_range_did_not_change_pixels",
            decision["modes"]["full"]["reasons"],
        )

    def test_report_validation_recomputes_decision_and_rejects_nonfinite(self) -> None:
        modes = fixture_modes()
        report = {
            "blender": {},
            "configuration": {},
            "decision": screen.evaluate_invalidation(modes),
            "experimental_only": True,
            "host": {},
            "kind": screen.KIND,
            "limitations": [],
            "modes": modes,
            "scene": {},
            "schema_version": screen.SCHEMA_VERSION,
            "warmup": {},
        }
        screen.validate_report(report)

        changed = copy.deepcopy(report)
        changed["decision"]["valid_narrower_modes"] = []
        with self.assertRaisesRegex(screen.ScreenError, "stored decision"):
            screen.validate_report(changed)

        changed = copy.deepcopy(report)
        changed["modes"]["none"]["renders"][0]["wall_s"] = float("nan")
        with self.assertRaisesRegex(screen.ScreenError, "wall_s"):
            screen.validate_report(changed)


if __name__ == "__main__":
    unittest.main()
