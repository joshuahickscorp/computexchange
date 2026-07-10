#!/usr/bin/env python3
"""Cheap tests for the speculative render ladder."""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_speculative_render_ladder as ladder  # noqa: E402


class SpeculativeRenderLadderTest(unittest.TestCase):
    def test_classify_requires_worst_tile(self):
        self.assertEqual(ladder.classify({"quality": 0.999, "worst_tile_ssim": 0.70}), "fail")
        self.assertEqual(ladder.classify({"quality": 0.95, "worst_tile_ssim": 0.90}), "preview")
        self.assertEqual(ladder.classify({"quality": 0.99, "worst_tile_ssim": 0.96}), "delivery")

    def test_gate_row_models_escalation_for_fail(self):
        gate = ladder.gate_row({
            "scene": "scene_monkey.xml",
            "samples": 4,
            "ref_samples": 4096,
            "ref_time_s": 5.0,
            "draft_time_s": 0.5,
            "quality": 0.99,
            "worst_tile_ssim": 0.70,
            "p5_tile_ssim": 0.80,
            "tile_count": 64,
        })
        self.assertEqual(gate["tier"], "fail")
        self.assertEqual(gate["action"], "reject_or_refine")
        self.assertLess(gate["whole_frame_escalated_speedup_x"], 1.0)

    def test_gate_tile_refinement_row_uses_actual_final_quality(self):
        gate = ladder.gate_tile_refinement_row({
            "scene": "scene_cube_volume.xml",
            "device": "CUDA",
            "ref_samples": 4096,
            "draft_samples": 16,
            "refine_samples": 32,
            "ref_time_s": 8.0,
            "draft_time_s": 1.0,
            "crop_product_time_s": 1.0,
            "product_total_time_s": 2.0,
            "product_speedup_vs_ref": 4.0,
            "grid": 8,
            "selected_tile_count": 4,
            "refined_tile_fraction": 0.0625,
            "failed_tile_count": 6,
            "crop_mode": "cx_batch_crop_manifest",
            "draft": {"worst_tile_ssim": 0.93},
            "final": {
                "quality": 0.994,
                "worst_tile_ssim": 0.946,
                "p5_tile_ssim": 0.966,
                "tile_count": 64,
            },
        })
        self.assertEqual(gate["variant"], "tile_refine")
        self.assertEqual(gate["tier"], "preview")
        self.assertEqual(gate["action"], "refine_more_or_fallback_for_delivery")
        self.assertEqual(gate["accepted_tile_fraction_actual"], 0.9375)
        self.assertEqual(gate["net_speedup_if_shipped_x"], 4.0)


if __name__ == "__main__":
    unittest.main()
