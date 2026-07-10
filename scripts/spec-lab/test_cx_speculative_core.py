#!/usr/bin/env python3
"""Tests for the CX-native speculative execution core."""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import cx_speculative_core as cx  # noqa: E402


class CxSpeculativeCoreTest(unittest.TestCase):
    def test_engine_accepts_and_repairs_exactly(self):
        units = [
            cx.SpecUnit("u0", "number", 1),
            cx.SpecUnit("u1", "number", 2),
            cx.SpecUnit("u2", "number", 3),
        ]

        def baseline(unit):
            return unit.payload * 10

        def draft(unit):
            # u1 intentionally misses so the repair path is exercised.
            guess = 999 if unit.unit_id == "u1" else baseline(unit)
            return cx.DraftProposal(unit=unit, draft=guess)

        def verify(proposal):
            truth = baseline(proposal.unit)
            return cx.Verification(accepted=proposal.draft == truth, truth=truth)

        def repair(_proposal, verification):
            return cx.RepairResult(output=verification.truth)

        engine = cx.SpeculativeEngine(
            branch_id="test",
            modality="number",
            draft=draft,
            verify=verify,
            repair=repair,
            baseline=baseline,
        )
        outputs, receipt = engine.run(units)
        self.assertEqual(outputs, [10, 20, 30])
        self.assertTrue(receipt.exact)
        self.assertEqual(receipt.accepted_units, 2)
        self.assertEqual(receipt.repaired_units, 1)
        self.assertEqual(receipt.attempted_units, 3)
        self.assertEqual(receipt.fallback_units, 0)

    def test_decision_prunes_low_acceptance(self):
        receipt = cx.SpecReceipt(
            branch_id="low",
            modality="token",
            units=10,
            accepted_units=0,
            repaired_units=10,
            rejected_units=10,
            draft_s=0.1,
            verify_s=0.1,
            repair_s=0.1,
            baseline_s=1.0,
            speculative_s=0.3,
            speedup_x=3.0,
            exact=True,
            quality_gate=True,
        )
        self.assertEqual(cx.decide_branch(receipt), "prune")

    def test_engine_gates_units_to_direct_fallback(self):
        units = [
            cx.SpecUnit("u0", "number", 1),
            cx.SpecUnit("u1", "number", 2),
            cx.SpecUnit("u2", "number", 3),
        ]
        fallback_ids = []

        def baseline(unit):
            return unit.payload * 10

        def draft(unit):
            return cx.DraftProposal(unit=unit, draft=baseline(unit))

        def verify(proposal):
            truth = baseline(proposal.unit)
            return cx.Verification(accepted=proposal.draft == truth, truth=truth)

        def repair(_proposal, verification):
            return cx.RepairResult(output=verification.truth)

        def should_speculate(unit):
            return unit.unit_id != "u1"

        def fallback(unit):
            fallback_ids.append(unit.unit_id)
            return baseline(unit)

        engine = cx.SpeculativeEngine(
            branch_id="gated",
            modality="number",
            draft=draft,
            verify=verify,
            repair=repair,
            baseline=baseline,
            should_speculate=should_speculate,
            fallback=fallback,
        )
        outputs, receipt = engine.run(units)
        self.assertEqual(outputs, [10, 20, 30])
        self.assertEqual(fallback_ids, ["u1"])
        self.assertTrue(receipt.exact)
        self.assertEqual(receipt.attempted_units, 2)
        self.assertEqual(receipt.fallback_units, 1)
        self.assertEqual(receipt.accepted_units, 2)
        self.assertAlmostEqual(receipt.accepted_attempt_fraction, 1.0)
        self.assertAlmostEqual(receipt.fallback_fraction, 1 / 3)


if __name__ == "__main__":
    unittest.main()
