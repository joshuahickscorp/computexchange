#!/usr/bin/env python3
"""Local correctness checks for the render SpecEngine adapter.

Everything here is SYNTHETIC (fixture seconds + SSIMs) — no GPU, no Blender. It proves the
accounting/decision logic and the canonical-contract shape, NOT any performance number.
"""

import os
import math
import sys
import unittest
from dataclasses import replace
from unittest import mock

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
                                repair_s=8.0, repair_clears_tier=True,
                                evidence=rsa.SYNTHETIC),
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
        self.assertFalse(rec.delivery_eligible)  # synthetic evidence can never ship

    def test_receipt_exposes_canonical_contract(self):
        units = [rsa.TileMeasurement("t0", 1.0, 0.1, 8.0, 0.99, 0.97, evidence=rsa.SYNTHETIC)]
        d = rsa.RenderSpecAdapter().receipt_from_measurements(units).to_dict()
        rsa.assert_canonical(d)  # raises if any Branch A field is missing
        self.assertFalse(d["artifact_verified"])
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
        rec = rsa.RenderSpecAdapter().from_stack_metrics(
            metrics, artifact_verified=True
        )
        self.assertEqual(rec.evidence, rsa.MEASURED)
        self.assertTrue(rec.delivery_eligible)
        self.assertTrue(rec.to_dict()["artifact_verified"])
        self.assertAlmostEqual(rec.speedup_vs_baseline, 40.0 / 6.0, places=6)

    def test_preview_gate_emits_preview_not_strict_delivery(self):
        adapter = rsa.RenderSpecAdapter(
            tier=rsa.QualityTier(
                global_min=0.95, worst_tile_min=0.90, canonical_tier="preview"
            )
        )
        rec = adapter.receipt_from_measurements(
            [rsa.TileMeasurement(
                "preview", 1.0, 0.1, 8.0, 0.96, 0.91,
                evidence=rsa.MEASURED,
            )],
            artifact_verified=True,
        )
        wire = rec.to_dict()
        self.assertTrue(rec.quality_gate)
        self.assertEqual(wire["quality_tier"], "preview")
        self.assertTrue(wire["artifact_verified"])

    def test_failing_quality_gate_blocks_delivery(self):
        metrics = {
            "T_ref_s": 40.0, "T_stack_s": 3.0, "quality": 0.93,  # below global tier
            "worst_tile_ssim": 0.60, "modeled": False,
        }
        rec = rsa.RenderSpecAdapter().from_stack_metrics(metrics)
        self.assertFalse(rec.quality_gate)
        self.assertFalse(rec.delivery_eligible)
        self.assertFalse(rec.to_dict()["artifact_verified"])

    def test_nan_and_infinite_quality_fail_closed(self):
        tier = rsa.QualityTier()
        for bad in (math.nan, math.inf, -math.inf):
            with self.subTest(bad=bad):
                self.assertFalse(tier.clears(global_ssim=bad, worst_tile_ssim=0.96))
                self.assertFalse(tier.clears(global_ssim=0.99, worst_tile_ssim=bad))

    def test_failed_tile_without_verified_repair_cannot_claim_delivery(self):
        unit = rsa.TileMeasurement(
            "failed", draft_s=1.0, verify_s=0.1, baseline_s=8.0,
            global_ssim=0.90, worst_tile_ssim=0.70, repair_s=8.0,
            repair_clears_tier=False, evidence=rsa.MEASURED,
        )
        rec = rsa.RenderSpecAdapter().receipt_from_measurements([unit])
        self.assertFalse(rec.quality_gate)
        self.assertFalse(rec.delivery_eligible)
        self.assertEqual(rec.repaired_units, 0)
        self.assertEqual(rec.repair_cost, 8.0)
        self.assertEqual(rec.meta["unresolved_failed_units"], 1)

    def test_accepted_tile_cannot_hide_reported_repair_work(self):
        unit = rsa.TileMeasurement(
            "accepted-with-repair", 1.0, 0.1, 8.0, 0.99, 0.97,
            repair_s=2.0, repair_clears_tier=True, evidence=rsa.SYNTHETIC,
        )
        rec = rsa.RenderSpecAdapter().receipt_from_measurements([unit])
        self.assertEqual(rec.accepted_units, 1)
        self.assertEqual(rec.repaired_units, 0)
        self.assertEqual(rec.repair_cost, 2.0)

    def test_measurement_receipt_is_bounded_and_requires_baseline(self):
        with self.assertRaises(ValueError):
            rsa.TileMeasurement("bad", 1.0, 0.1, 0.0, 0.99, 0.97)
        units = (
            rsa.TileMeasurement(str(i), 1.0, 0.1, 2.0, 0.99, 0.97)
            for i in range(3)
        )
        with mock.patch.object(rsa, "MAX_RENDER_RECEIPT_UNITS", 2):
            with self.assertRaisesRegex(ValueError, "safety cap"):
                rsa.RenderSpecAdapter().receipt_from_measurements(units)

    def test_measurement_ids_and_metadata_are_unambiguous_and_bounded(self):
        duplicate = [
            rsa.TileMeasurement("same", 1.0, 0.1, 8.0, 0.99, 0.97),
            rsa.TileMeasurement("same", 1.0, 0.1, 8.0, 0.99, 0.97),
        ]
        with self.assertRaisesRegex(ValueError, "ids must be unique"):
            rsa.RenderSpecAdapter().receipt_from_measurements(duplicate)
        with self.assertRaisesRegex(ValueError, "keys must be strings"):
            rsa.RenderSpecAdapter().receipt_from_measurements(
                [duplicate[0]], meta={1: "ambiguous", "1": "wire key"}
            )

    def test_bare_measured_metrics_do_not_manufacture_artifact_proof(self):
        metrics = {
            "T_ref_s": 40.0, "T_stack_s": 6.0, "quality": 0.991,
            "worst_tile_ssim": 0.962, "modeled": False,
        }
        unbound = rsa.RenderSpecAdapter().from_stack_metrics(metrics)
        self.assertTrue(unbound.quality_gate)
        self.assertFalse(unbound.artifact_verified)
        self.assertFalse(unbound.delivery_eligible)
        with self.assertRaises(TypeError):
            rsa.RenderSpecAdapter().from_stack_metrics(
                metrics, artifact_verified="true"
            )

    def test_stack_metrics_reject_nonfinite_and_contradictory_accounting(self):
        base = {
            "T_ref_s": 40.0, "T_stack_s": 6.0, "quality": 0.991,
            "worst_tile_ssim": 0.962, "modeled": False,
        }
        for key in ("T_ref_s", "T_stack_s", "quality", "worst_tile_ssim"):
            with self.subTest(key=key):
                bad = dict(base, **{key: math.nan})
                with self.assertRaises(ValueError):
                    rsa.RenderSpecAdapter().from_stack_metrics(bad)
        with self.assertRaises(TypeError):
            rsa.RenderSpecAdapter().from_stack_metrics(dict(base, modeled="false"))
        with self.assertRaises(ValueError):
            rsa.RenderSpecAdapter().from_stack_metrics(dict(base, net_speedup=99.0))

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
        self.assertFalse(canon.artifact_verified)
        self.assertFalse(d["artifact_verified"])
        self.assertAlmostEqual(canon.total_product_time, receipt.speculative_s)
        self.assertEqual(canon.baseline_source, receipt.baseline_source)
        self.assertIn("baseline_comparable", canon.meta["engine_benchmark"])
        with self.assertRaisesRegex(ValueError, "cannot relabel"):
            adapter.from_speculative_receipt(receipt, evidence=rsa.MEASURED)

    def test_canonical_guard_rejects_baseline_and_proof_contradictions(self):
        wire = rsa.RenderSpecAdapter().receipt_from_measurements([
            rsa.TileMeasurement(
                "t", 1.0, 0.1, 8.0, 0.99, 0.97, evidence=rsa.SYNTHETIC
            )
        ]).to_dict()
        absent_with_time = dict(
            wire,
            baseline_source="absent",
            speedup_vs_baseline=None,
        )
        with self.assertRaisesRegex(AssertionError, "zero time"):
            rsa.assert_canonical(absent_with_time)

        forged_proof = dict(
            wire,
            evidence="measured",
            artifact_verified=True,
            quality_gate=False,
        )
        with self.assertRaisesRegex(AssertionError, "passing gate"):
            rsa.assert_canonical(forged_proof)

    def test_direct_receipt_cannot_infer_proof_or_emit_invalid_scalar_types(self):
        rec = rsa.RenderSpecAdapter().receipt_from_measurements([
            rsa.TileMeasurement("t", 1.0, 0.1, 8.0, 0.99, 0.97,
                                evidence=rsa.SYNTHETIC)
        ])
        with self.assertRaises(ValueError):
            replace(rec, artifact_verified=True)
        with self.assertRaises(TypeError):
            replace(rec, units=True)
        with self.assertRaises(ValueError):
            replace(rec, draft_cost=True)
        with self.assertRaises(ValueError):
            replace(rec, global_ssim=math.nan)
        with self.assertRaises(ValueError):
            replace(rec, units=rsa.MAX_RENDER_RECEIPT_UNITS + 1)

    def test_stack_metrics_reject_coerced_optionals_and_oversized_tile_count(self):
        base = {
            "T_ref_s": 40.0, "T_stack_s": 6.0, "quality": 0.991,
            "worst_tile_ssim": 0.962, "modeled": False,
        }
        for key, value in (
            ("reproject_accept_frac", "0.5"),
            ("mean_disoccluded_frac", True),
            ("fixed_overhead_s", "1.0"),
        ):
            with self.subTest(key=key):
                with self.assertRaises(TypeError):
                    rsa.RenderSpecAdapter().from_stack_metrics({**base, key: value})
        with self.assertRaisesRegex(ValueError, "unit safety cap"):
            rsa.RenderSpecAdapter().from_stack_metrics({
                **base,
                "frames": rsa.MAX_RENDER_RECEIPT_UNITS // 64 + 1,
                "repair_total_s": 1.0,
                "repaired_tile_count": 0,
            })

    def test_p5_tier_can_reject_when_configured(self):
        tier = rsa.QualityTier(global_min=0.98, worst_tile_min=0.95, p5_min=0.97)
        adapter = rsa.RenderSpecAdapter(tier=tier)
        # global + worst clear, but p5 fails -> unit is repaired
        u = rsa.TileMeasurement("t", 1.0, 0.1, 8.0, global_ssim=0.99, worst_tile_ssim=0.96,
                                p5_ssim=0.90, repair_s=8.0, repair_clears_tier=True,
                                evidence=rsa.SYNTHETIC)
        self.assertFalse(adapter.accepts(u))
        self.assertIn("p5>=0.97", tier.label)


if __name__ == "__main__":
    unittest.main()
