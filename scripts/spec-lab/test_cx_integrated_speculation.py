#!/usr/bin/env python3
"""Focused correctness checks for the integrated render/token receipt."""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import cx_integrated_speculation as integrated  # noqa: E402
import run_spec_render_token_end_to_end as runner  # noqa: E402


class IntegratedSpeculationTest(unittest.TestCase):
    def test_exact_manifest_stream_is_verified(self):
        job = integrated.RenderSpecJob(
            "test", "video", "classroom", "3840x2160", 8, "render", "token"
        )
        row = runner.run_manifest_speculation(runner.manifest_stream(job, 96), 12, [512, 128, 32, 8, 2])
        self.assertTrue(row["exact"])
        self.assertGreater(row["target_call_reduction_x"], 1.0)
        self.assertIn("json_template", row["proposal_sources"])

    def test_complete_measured_delivery_grows(self):
        decision = integrated.RenderVerifier.decide(
            token_exact=True, global_ssim=0.99, worst_tile_ssim=0.96, render_modeled=False
        )
        self.assertEqual(decision.action, "grow")

    def test_missing_worst_tile_blocks_delivery_claim(self):
        decision = integrated.RenderVerifier.decide(
            token_exact=True, global_ssim=0.99, worst_tile_ssim=None, render_modeled=False
        )
        self.assertEqual(decision.action, "park")

    def test_stack_metrics_preserve_modeled_flag(self):
        receipt = runner.normalize_stack_metrics(
            {"T_ref_s": 20.0, "T_stack_s": 5.0, "quality": 0.99,
             "worst_tile_ssim": 0.96, "modeled": True},
            "pod:abc",
        )
        self.assertEqual(receipt["render_baseline_s"], 20.0)
        self.assertTrue(receipt["render_modeled"])


if __name__ == "__main__":
    unittest.main()
