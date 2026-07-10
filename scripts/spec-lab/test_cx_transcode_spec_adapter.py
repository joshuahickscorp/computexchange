#!/usr/bin/env python3
"""Local correctness checks for the transcode SpecEngine adapter.

Everything here is SYNTHETIC (fixture seconds + SSIMs) — no ffmpeg is invoked. It proves the
segmenting plan, the accounting/decision logic and the canonical receipt.rs wire shape, NOT
any performance number. The real numbers come only from `--codec ... --mode ...` runs with a
real local ffmpeg (ledgered to docs/speed-lane-reports/spec-lab/transcode_spec_ledger.jsonl).
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import cx_transcode_spec_adapter as tsa  # noqa: E402


def seg(i, *, accepted=True, draft=0.5, verify=0.1, ssim=0.98, repair=0.0,
        evidence=tsa.SYNTHETIC):
    return tsa.SegmentResult(
        seg_id=f"seg_{i:03d}", draft_encode_s=draft, verify_s=verify,
        accepted=accepted, draft_ssim=ssim, repair_encode_s=repair, evidence=evidence,
    )


class SegmentPlanTest(unittest.TestCase):
    def test_exact_division(self):
        self.assertEqual(tsa.plan_segments(12.0, 2.0), 6)

    def test_trailing_partial_segment(self):
        self.assertEqual(tsa.plan_segments(13.0, 2.0), 7)

    def test_single_short_clip(self):
        self.assertEqual(tsa.plan_segments(1.5, 2.0), 1)

    def test_invalid_inputs_raise(self):
        with self.assertRaises(ValueError):
            tsa.plan_segments(0.0, 2.0)
        with self.assertRaises(ValueError):
            tsa.plan_segments(10.0, 0.0)


class AccountingTest(unittest.TestCase):
    def test_accept_repair_costs_and_one_ratio_headline(self):
        # 4 accepted + 1 rejected->repaired. Fixture seconds.
        segments = [seg(i) for i in range(4)] + [
            seg(4, accepted=False, ssim=0.91, repair=4.0)
        ]
        rec = tsa.build_receipt(
            segments, baseline_wall_s=25.0, segment_wall_s=0.2, concat_wall_s=0.1,
            exact=False, baseline_source="modeled",
        )
        self.assertEqual(rec.units, 5)
        self.assertEqual(rec.accepted_units, 4)
        self.assertEqual(rec.repaired_units, 1)
        self.assertAlmostEqual(rec.accepted_fraction, 0.8)
        self.assertAlmostEqual(rec.repaired_fraction, 0.2)
        # draft cost carries the FULL cheap path: segmentation + drafts + concat
        self.assertAlmostEqual(rec.draft_cost_s, 0.2 + 5 * 0.5 + 0.1)
        self.assertAlmostEqual(rec.verify_cost_s, 5 * 0.1)
        self.assertAlmostEqual(rec.repair_cost_s, 4.0)  # only the rejected segment
        self.assertAlmostEqual(rec.total_product_time_s, 2.8 + 0.5 + 4.0)
        # headline is ONE ratio baseline/total — never a product of per-segment ratios
        self.assertAlmostEqual(rec.speedup_vs_baseline, 25.0 / 7.3, places=6)

    def test_repair_disabled_ships_preview_not_delivery(self):
        segments = [seg(0), seg(1, accepted=False, ssim=0.90, repair=4.0)]
        rec = tsa.build_receipt(
            segments, baseline_wall_s=10.0, segment_wall_s=0.1, concat_wall_s=0.1,
            exact=False, repair_enabled=False, baseline_source="modeled",
        )
        self.assertEqual(rec.quality_tier, "preview")  # below-gate draft was shipped
        self.assertEqual(rec.repaired_units, 0)
        self.assertAlmostEqual(rec.repair_cost_s, 0.0)  # repair never charged when disabled

    def test_all_accepted_is_delivery_with_zero_repair(self):
        rec = tsa.build_receipt(
            [seg(i) for i in range(3)], baseline_wall_s=9.0, segment_wall_s=0.1,
            concat_wall_s=0.1, exact=False, baseline_source="modeled",
        )
        self.assertEqual(rec.quality_tier, "delivery")
        self.assertAlmostEqual(rec.repair_cost_s, 0.0)
        self.assertAlmostEqual(rec.repaired_fraction, 0.0)

    def test_repaired_to_baseline_recipe_is_delivery(self):
        rec = tsa.build_receipt(
            [seg(0, accepted=False, ssim=0.5, repair=3.0)], baseline_wall_s=5.0,
            segment_wall_s=0.1, concat_wall_s=0.1, exact=False, baseline_source="modeled",
        )
        # mirrors the render lane: a repaired unit is delivered at the baseline recipe
        self.assertEqual(rec.quality_tier, "delivery")
        self.assertAlmostEqual(rec.repaired_fraction, 1.0)

    def test_empty_delivery_is_fail(self):
        rec = tsa.build_receipt(
            [], baseline_wall_s=5.0, segment_wall_s=0.0, concat_wall_s=0.0,
            exact=False, baseline_source="modeled",
        )
        self.assertEqual(rec.quality_tier, "fail")
        self.assertEqual(rec.units, 0)

    def test_speedup_null_when_baseline_absent_or_zero_total(self):
        rec = tsa.build_receipt(
            [seg(0)], baseline_wall_s=0.0, segment_wall_s=0.1, concat_wall_s=0.1,
            exact=False, baseline_source="absent",
        )
        self.assertIsNone(rec.speedup_vs_baseline)  # a speedup is never fabricated
        d = rec.to_dict()
        self.assertIsNone(d["speedup_vs_baseline"])

    def test_evidence_worst_label_dominates(self):
        segments = [seg(0, evidence=tsa.MEASURED), seg(1, evidence=tsa.SYNTHETIC)]
        rec = tsa.build_receipt(
            segments, baseline_wall_s=5.0, segment_wall_s=0.1, concat_wall_s=0.1,
            exact=False, baseline_source="modeled",
        )
        self.assertEqual(rec.evidence, tsa.SYNTHETIC)  # dirtiest unit wins
        segments = [seg(0, evidence=tsa.MEASURED), seg(1, evidence=tsa.MODELED)]
        rec = tsa.build_receipt(
            segments, baseline_wall_s=5.0, segment_wall_s=0.1, concat_wall_s=0.1,
            exact=False, baseline_source="modeled",
        )
        self.assertEqual(rec.evidence, tsa.MODELED)

    def test_ssim_gated_is_never_exact_unless_proven(self):
        # `exact` comes ONLY from the caller's decoded-frame MD5 proof, never from SSIM.
        rec = tsa.build_receipt(
            [seg(0, ssim=1.0)], baseline_wall_s=5.0, segment_wall_s=0.1, concat_wall_s=0.1,
            exact=False, baseline_source="modeled",
        )
        self.assertFalse(rec.to_dict()["exact"])


class CanonicalShapeTest(unittest.TestCase):
    """The emitted dict must satisfy spec-engine/src/receipt.rs's deserializer — the Python
    mimic of serde strictness (required keys, enum vocab, numeric/bool types)."""

    def _receipt_dict(self):
        segments = [seg(i) for i in range(4)] + [seg(4, accepted=False, ssim=0.91, repair=4.0)]
        return tsa.build_receipt(
            segments, baseline_wall_s=25.0, segment_wall_s=0.2, concat_wall_s=0.1,
            exact=False, baseline_source="modeled",
        ).to_dict()

    def test_required_and_defaulted_receipt_rs_fields_present(self):
        d = self._receipt_dict()
        tsa.assert_canonical(d)
        for k in tsa.CANONICAL_REQUIRED_FIELDS + tsa.CANONICAL_DEFAULTED_FIELDS:
            self.assertIn(k, d)

    def test_enum_values_are_receipt_rs_vocab(self):
        d = self._receipt_dict()
        self.assertIn(d["quality_tier"], tsa.QUALITY_TIERS)
        self.assertIn(d["evidence"], tsa.EVIDENCE_WIRE)
        self.assertIn(d["baseline_source"], tsa.BASELINE_SOURCES)
        self.assertEqual(d["modality"], "transcode")
        self.assertIsInstance(d["exact"], bool)
        self.assertIsInstance(d["units"], int)

    def test_total_is_sum_of_charged_parts(self):
        d = self._receipt_dict()
        self.assertAlmostEqual(
            d["total_product_time_s"],
            d["draft_cost_s"] + d["verify_cost_s"] + d["repair_cost_s"], places=4,
        )

    def test_json_roundtrip_value_stable(self):
        d = self._receipt_dict()
        self.assertEqual(json.loads(json.dumps(d)), d)

    def test_assert_canonical_rejects_missing_and_bad_values(self):
        d = self._receipt_dict()
        for k in tsa.CANONICAL_REQUIRED_FIELDS:
            broken = dict(d)
            del broken[k]
            with self.assertRaises(AssertionError, msg=f"missing {k} must fail"):
                tsa.assert_canonical(broken)
        bad_tier = dict(d, quality_tier="g>=0.98,wt>=0.95")  # a gate SPEC is not a tier enum
        with self.assertRaises(AssertionError):
            tsa.assert_canonical(bad_tier)
        bad_evidence = dict(d, evidence="MEASURED")  # wire form must be lower-case
        with self.assertRaises(AssertionError):
            tsa.assert_canonical(bad_evidence)
        bad_exact = dict(d, exact="true")  # must be a real bool
        with self.assertRaises(AssertionError):
            tsa.assert_canonical(bad_exact)
        hidden_cost = dict(d, total_product_time_s=d["total_product_time_s"] + 1.0)
        with self.assertRaises(AssertionError):
            tsa.assert_canonical(hidden_cost)  # nothing may hide outside draft+verify+repair

    def test_simulate_is_synthetic_and_canonical(self):
        d = tsa.simulate()
        tsa.assert_canonical(d)
        self.assertEqual(d["evidence"], "synthetic")   # never mistakable for a measurement
        self.assertEqual(d["baseline_source"], "modeled")
        self.assertEqual(d["quality_tier"], "delivery")
        self.assertFalse(d["exact"])                   # SSIM-gated fixture: not lossless

    def test_per_segment_transparency_in_details(self):
        d = self._receipt_dict()
        per = d["details"]["per_segment"]
        self.assertEqual(len(per), 5)
        rejected = [p for p in per if not p["accepted"]]
        self.assertEqual(len(rejected), 1)
        self.assertAlmostEqual(rejected[0]["repair_encode_s"], 4.0)


if __name__ == "__main__":
    unittest.main()
