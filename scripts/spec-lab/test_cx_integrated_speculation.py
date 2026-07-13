#!/usr/bin/env python3
"""Focused correctness checks for the integrated render/token receipt."""

import os
import math
import sys
import tempfile
import unittest
from pathlib import Path

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
            token_exact=True, global_ssim=0.99, worst_tile_ssim=0.96,
            render_modeled=False,
            evidence_type=integrated.MEASURED_SAME_JOB_EVIDENCE,
            token_modeled=False,
            artifact_verified=True,
        )
        self.assertEqual(decision.action, "grow")

    def test_unbound_quality_metrics_cannot_grow(self):
        decision = integrated.RenderVerifier.decide(
            token_exact=True, global_ssim=0.99, worst_tile_ssim=0.96,
            render_modeled=False,
            evidence_type=integrated.MEASURED_SAME_JOB_EVIDENCE,
            token_modeled=False,
        )
        self.assertEqual(decision.action, "park")
        self.assertIn("bound", decision.reason)

    def test_missing_worst_tile_blocks_delivery_claim(self):
        decision = integrated.RenderVerifier.decide(
            token_exact=True, global_ssim=0.99, worst_tile_ssim=None,
            render_modeled=False,
            evidence_type=integrated.MEASURED_SAME_JOB_EVIDENCE,
            token_modeled=False,
        )
        self.assertEqual(decision.action, "park")

    def test_nonfinite_or_out_of_range_quality_fails_closed(self):
        for bad in (math.nan, math.inf, -math.inf, -0.01, 1.01):
            with self.subTest(bad=bad):
                decision = integrated.RenderVerifier.decide(
                    token_exact=True,
                    global_ssim=bad,
                    worst_tile_ssim=0.96,
                    render_modeled=False,
                    evidence_type=integrated.MEASURED_SAME_JOB_EVIDENCE,
                    token_modeled=False,
                )
                self.assertEqual(decision.action, "kill_correctness")
                decision = integrated.RenderVerifier.decide(
                    token_exact=True,
                    global_ssim=0.99,
                    worst_tile_ssim=bad,
                    render_modeled=False,
                    evidence_type=integrated.MEASURED_SAME_JOB_EVIDENCE,
                    token_modeled=False,
                )
                self.assertEqual(decision.action, "kill_correctness")

    def test_stack_metrics_preserve_modeled_flag(self):
        receipt = runner.normalize_stack_metrics(
            {"T_ref_s": 20.0, "T_stack_s": 5.0, "quality": 0.99,
             "worst_tile_ssim": 0.96, "modeled": True},
            "pod:abc",
        )
        self.assertEqual(receipt["render_baseline_s"], 20.0)
        self.assertTrue(receipt["render_modeled"])

    def test_receipt_derives_decision_and_labels_modeled_claim(self):
        job = integrated.RenderSpecJob("j", "still", "s", "64x64", 1, "r", "t")
        receipt = integrated.RenderSpecReceipt(
            job=job, token_baseline_s=1.0, token_spec_s=0.1,
            render_baseline_s=10.0, render_spec_s=2.0,
            global_ssim=0.99, worst_tile_ssim=0.96,
            token_exact=True, render_modeled=True, evidence_type="fixture",
        )
        self.assertEqual(receipt.decision.action, "park")
        self.assertIn("MODELED", receipt.to_dict()["claim_scope"])

    def test_unattested_evidence_cannot_grow_even_when_quality_passes(self):
        job = integrated.RenderSpecJob("j", "still", "s", "64x64", 1, "r", "t")
        receipt = integrated.RenderSpecReceipt(
            job=job, token_baseline_s=1.0, token_spec_s=0.1,
            render_baseline_s=10.0, render_spec_s=2.0,
            global_ssim=0.99, worst_tile_ssim=0.96,
            token_exact=True, render_modeled=False, evidence_type="fixture",
            token_modeled=False,
        )
        self.assertEqual(receipt.decision.action, "park")
        self.assertIn("UNATTESTED", receipt.to_dict()["claim_scope"])

    def test_current_protocol_token_timing_stays_parked(self):
        job = integrated.RenderSpecJob("j", "still", "s", "64x64", 1, "r", "t")
        receipt = integrated.RenderSpecReceipt(
            job=job, token_baseline_s=1.0, token_spec_s=0.1,
            render_baseline_s=10.0, render_spec_s=2.0,
            global_ssim=0.99, worst_tile_ssim=0.96,
            token_exact=True, render_modeled=False,
            evidence_type=integrated.MEASURED_SAME_JOB_EVIDENCE,
        )
        self.assertEqual(receipt.decision.action, "park")
        self.assertIn("model-backed", receipt.decision.reason)

    def test_zero_time_receipt_is_rejected(self):
        job = integrated.RenderSpecJob("j", "still", "s", "64x64", 1, "r", "t")
        with self.assertRaises(ValueError):
            integrated.RenderSpecReceipt(
                job=job, token_baseline_s=0.0, token_spec_s=0.1,
                render_baseline_s=10.0, render_spec_s=2.0,
                global_ssim=0.99, worst_tile_ssim=0.96,
                token_exact=True, render_modeled=False,
                evidence_type=integrated.MEASURED_SAME_JOB_EVIDENCE,
            )

    def test_normalizer_rejects_string_bool_and_nonfinite(self):
        base = {"T_ref_s": 20.0, "T_stack_s": 5.0, "quality": 0.99,
                "worst_tile_ssim": 0.96, "modeled": False}
        with self.assertRaises(TypeError):
            runner.normalize_stack_metrics(dict(base, modeled="false"), "x")
        with self.assertRaises(ValueError):
            runner.normalize_stack_metrics(dict(base, quality=float("nan")), "x")
        with self.assertRaises(ValueError):
            runner.normalize_stack_metrics(base, "")

    def test_manifest_and_receipt_inputs_are_bounded_and_strict(self):
        job = integrated.RenderSpecJob("j", "still", "s", "64x64", 1, "r", "t")
        with self.assertRaises(ValueError):
            runner.manifest_stream(job, 0)
        with self.assertRaises(ValueError):
            runner.manifest_stream(job, runner.MAX_MANIFEST_EVENTS + 1)
        with self.assertRaises(ValueError):
            runner.run_manifest_speculation([], 0, [1])
        with self.assertRaises(ValueError):
            runner.run_manifest_speculation([1, 2], 0, [0])

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "receipt.json"
            path.write_text(
                '{"render_baseline_s":1,"render_baseline_s":2,"render_spec_s":1}'
            )
            with self.assertRaisesRegex(ValueError, "duplicate"):
                runner.load_render_receipt(path)

    def test_missing_external_render_provenance_defaults_to_parked(self):
        modeled, evidence = runner.render_provenance({})
        self.assertTrue(modeled)
        self.assertEqual(evidence, "unknown")
        with self.assertRaises(TypeError):
            runner.render_provenance({"render_modeled": "false"})

    def test_direct_verifier_rejects_truthy_non_boolean_flags(self):
        decision = integrated.RenderVerifier.decide(
            token_exact="true", global_ssim=0.99, worst_tile_ssim=0.96,
            render_modeled=False,
            evidence_type=integrated.MEASURED_SAME_JOB_EVIDENCE,
            token_modeled=False,
        )
        self.assertEqual(decision.action, "kill_correctness")

    def test_external_render_times_are_not_coerced(self):
        job = integrated.RenderSpecJob("j", "still", "s", "64x64", 1, "r", "t")
        for bad in (True, "1.0"):
            with self.subTest(bad=bad):
                with self.assertRaises(TypeError):
                    integrated.RenderSpecReceipt(
                        job=job, token_baseline_s=1.0, token_spec_s=0.1,
                        render_baseline_s=bad, render_spec_s=2.0,
                        global_ssim=0.99, worst_tile_ssim=0.96,
                        token_exact=True, render_modeled=False,
                        evidence_type=integrated.MEASURED_SAME_JOB_EVIDENCE,
                    )


if __name__ == "__main__":
    unittest.main()
