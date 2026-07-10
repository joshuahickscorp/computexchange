#!/usr/bin/env python3
"""Local correctness checks for the render SpecEngine adapter.

Everything here is SYNTHETIC (fixture seconds + SSIMs) — no GPU, no Blender. It proves the
accounting/decision logic and the canonical-contract shape, NOT any performance number.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import cx_render_spec_adapter as rsa  # noqa: E402
import cx_speculative_core as core  # noqa: E402


class RenderSpecAdapterTest(unittest.TestCase):
    def test_accept_reject_and_headline_ratio(self):
        # 3 units clear the tier, 1 fails worst-tile and is repaired (SYNTHETIC seconds).
        units = [
            rsa.TileMeasurement("t0", draft_s=1.0, verify_s=0.1, baseline_s=8.0,
                                global_ssim=0.99, worst_tile_ssim=0.97, evidence=rsa.SYNTHETIC),
            rsa.TileMeasurement("t1", draft_s=1.0, verify_s=0.1, baseline_s=8.0,
                                global_ssim=0.99, worst_tile_ssim=0.96, evidence=rsa.SYNTHETIC),
            rsa.TileMeasurement("t2", draft_s=1.0, verify_s=0.1, baseline_s=8.0,
                                global_ssim=0.985, worst_tile_ssim=0.955, evidence=rsa.SYNTHETIC),
            rsa.TileMeasurement("t3", draft_s=1.0, verify_s=0.1, baseline_s=8.0,
                                global_ssim=0.97, worst_tile_ssim=0.80,  # fails worst-tile
                                repair_s=8.0, evidence=rsa.SYNTHETIC),
        ]
        rec = rsa.RenderSpecAdapter().receipt_from_measurements(units)
        self.assertEqual(rec.accepted_units, 3)
        self.assertEqual(rec.repaired_units, 1)
        self.assertAlmostEqual(rec.accepted_fraction, 0.75)
        self.assertAlmostEqual(rec.draft_cost, 4.0)
        self.assertAlmostEqual(rec.verify_cost, 0.4)
        self.assertAlmostEqual(rec.repair_cost, 8.0)          # only the failed unit's re-render
        self.assertAlmostEqual(rec.total_product_time, 12.4)  # draft+verify+repair
        self.assertAlmostEqual(rec.baseline_cost, 32.0)       # 4 * 8.0 reference
        # headline is ONE ratio: baseline / total_product_time (never a product of per-tile x)
        self.assertAlmostEqual(rec.speedup_vs_baseline, 32.0 / 12.4, places=6)

    def test_receipt_exposes_canonical_contract(self):
        units = [rsa.TileMeasurement("t0", 1.0, 0.1, 8.0, 0.99, 0.97, evidence=rsa.SYNTHETIC)]
        d = rsa.RenderSpecAdapter().receipt_from_measurements(units).to_dict()
        rsa.assert_canonical(d)  # raises if any Branch A field is missing
        for k in rsa.CANONICAL_FIELDS:
            self.assertIn(k, d)

    def test_modeled_stack_receipt_parks_not_delivers(self):
        # A modeled receipt must NOT be delivery-eligible even with a passing quality gate.
        metrics = {
            "T_ref_s": 40.0, "T_stack_s": 5.0, "quality": 0.991, "worst_tile_ssim": 0.962,
            "p5_tile_ssim": 0.972, "modeled": True, "reproject_accept_frac": 0.93,
            "mean_disoccluded_frac": 0.07, "fixed_overhead_s": 0.8, "net_speedup": 8.0,
            "device": "GPU/OPTIX", "scene": "classroom", "resolution": "3840x2160",
        }
        rec = rsa.RenderSpecAdapter().from_stack_metrics(metrics)
        self.assertEqual(rec.evidence, rsa.MODELED)
        self.assertTrue(rec.quality_gate)          # SSIMs clear the tier
        self.assertFalse(rec.delivery_eligible)    # ...but a modeled cost PARKS it
        self.assertAlmostEqual(rec.total_product_time, 5.0)   # == T_stack (crop not double-charged)
        self.assertAlmostEqual(rec.speedup_vs_baseline, 8.0, places=6)  # T_ref / T_stack
        self.assertAlmostEqual(rec.repair_cost, 0.8)
        self.assertAlmostEqual(rec.draft_cost, 4.2)  # T_stack - crop
        rsa.assert_canonical(rec.to_dict())

    def test_measured_stack_receipt_can_deliver(self):
        metrics = {
            "T_ref_s": 40.0, "T_stack_s": 6.0, "quality": 0.991, "worst_tile_ssim": 0.962,
            "p5_tile_ssim": 0.972, "modeled": False, "reproject_accept_frac": 1.0,
            "mean_disoccluded_frac": 0.0, "fixed_overhead_s": 0.0,
        }
        rec = rsa.RenderSpecAdapter().from_stack_metrics(metrics)
        self.assertEqual(rec.evidence, rsa.MEASURED)
        self.assertTrue(rec.delivery_eligible)
        self.assertAlmostEqual(rec.speedup_vs_baseline, 40.0 / 6.0, places=6)

    def test_failing_quality_gate_blocks_delivery(self):
        metrics = {
            "T_ref_s": 40.0, "T_stack_s": 3.0, "quality": 0.93,  # below global tier
            "worst_tile_ssim": 0.60, "modeled": False,
        }
        rec = rsa.RenderSpecAdapter().from_stack_metrics(metrics)
        self.assertFalse(rec.quality_gate)
        self.assertFalse(rec.delivery_eligible)

    def test_live_engine_receipt_is_same_canonical_shape(self):
        # Drive the SHARED cx_speculative_core engine with trivial render-shaped callables and
        # show its SpecReceipt maps onto the SAME canonical render receipt (token lane parity).
        adapter = rsa.RenderSpecAdapter()

        def draft(u):
            return core.DraftProposal(u, draft=u.payload)  # "reproject": echo the unit

        def verify(p):
            return core.Verification(accepted=True, truth=p.unit.payload, quality=1.0)

        def repair(p, v):
            return core.RepairResult(output=p.unit.payload)

        def baseline(u):
            return u.payload

        engine = adapter.build_engine(draft=draft, verify=verify, repair=repair, baseline=baseline)
        units = [core.SpecUnit(f"u{i}", "render", payload=i) for i in range(5)]
        _outputs, receipt = engine.run(units)
        canon = adapter.from_speculative_receipt(
            receipt, global_ssim=0.99, worst_tile_ssim=0.96, evidence=rsa.SYNTHETIC
        )
        d = canon.to_dict()
        rsa.assert_canonical(d)
        self.assertEqual(canon.modality, "render")
        self.assertEqual(canon.units, 5)
        self.assertTrue(canon.quality_gate)

    def test_p5_tier_can_reject_when_configured(self):
        tier = rsa.QualityTier(global_min=0.98, worst_tile_min=0.95, p5_min=0.97)
        adapter = rsa.RenderSpecAdapter(tier=tier)
        # global + worst clear, but p5 fails -> unit is repaired
        u = rsa.TileMeasurement("t", 1.0, 0.1, 8.0, global_ssim=0.99, worst_tile_ssim=0.96,
                                p5_ssim=0.90, repair_s=8.0, evidence=rsa.SYNTHETIC)
        self.assertFalse(adapter.accepts(u))
        self.assertIn("p5>=0.97", tier.label)


if __name__ == "__main__":
    unittest.main()
