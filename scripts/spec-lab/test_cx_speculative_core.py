#!/usr/bin/env python3
"""Tests for the CX-native speculative execution core."""

import os
import sys
import time
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
            speedup_x=1.0 / 0.3,
            exact=True,
            quality_gate=True,
        )
        self.assertEqual(cx.decide_branch(receipt), "park")

        attested = cx.SpecReceipt(
            branch_id="attested", modality="token", units=10,
            accepted_units=5, repaired_units=5, rejected_units=5,
            draft_s=0.1, verify_s=0.1, repair_s=0.1, overhead_s=0.0,
            baseline_s=0.6, speculative_s=0.3, speedup_x=2.0,
            exact=True, quality_gate=True, artifact_verified=True,
            evidence="measured",
        )
        self.assertEqual(cx.decide_branch(attested), "grow")

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

    def test_default_fallback_reuses_authoritative_baseline_once(self):
        calls = 0

        def baseline(unit):
            nonlocal calls
            calls += 1
            return unit.payload * 10

        engine = cx.SpeculativeEngine(
            branch_id="reuse", modality="number",
            draft=lambda u: cx.DraftProposal(u, u.payload),
            verify=lambda p: cx.Verification(True, p.draft),
            repair=lambda p, _v: cx.RepairResult(p.draft),
            baseline=baseline,
            should_speculate=lambda _u: False,
        )
        outputs, receipt = engine.run([
            cx.SpecUnit("u0", "number", 1), cx.SpecUnit("u1", "number", 2),
        ])
        self.assertEqual(outputs, [10, 20])
        self.assertEqual(calls, 2)  # one authoritative call per unit, never two
        self.assertEqual(receipt.fallback_units, 2)
        self.assertEqual(receipt.meta["baseline_reused_for_fallback_units"], 2)
        self.assertEqual(receipt.fallback_s, receipt.baseline_s)
        self.assertLessEqual(receipt.speedup_x, 1.0)

    def test_lying_verifier_never_releases_wrong_output(self):
        unit = cx.SpecUnit("u0", "number", 7)

        engine = cx.SpeculativeEngine(
            branch_id="lying",
            modality="number",
            draft=lambda u: cx.DraftProposal(u, 999),
            verify=lambda _p: cx.Verification(accepted=True, truth=70),
            repair=lambda _p, v: cx.RepairResult(v.truth),
            baseline=lambda u: u.payload * 10,
        )
        outputs, receipt = engine.run([unit])
        self.assertEqual(outputs, [70])  # authoritative fallback, never 999
        self.assertTrue(receipt.exact)  # final escaped artifact is authoritative
        self.assertFalse(receipt.meta["candidate_exact"])
        self.assertFalse(receipt.quality_gate)
        self.assertEqual(receipt.meta["disposition"], "fallback")
        self.assertEqual(receipt.fallback_units, 1)
        self.assertGreater(receipt.fallback_s, 0)
        self.assertLessEqual(receipt.speedup_x, 1.0)

    def test_broken_repair_and_callback_exception_fall_back(self):
        units = [cx.SpecUnit("u0", "number", 1), cx.SpecUnit("u1", "number", 2)]

        def draft(u):
            if u.unit_id == "u1":
                raise RuntimeError("drafter offline")
            return cx.DraftProposal(u, -1)

        engine = cx.SpeculativeEngine(
            branch_id="faults", modality="number", draft=draft,
            verify=lambda p: cx.Verification(False, p.unit.payload * 10),
            repair=lambda _p, _v: cx.RepairResult(-2),
            baseline=lambda u: u.payload * 10,
        )
        outputs, receipt = engine.run(units)
        self.assertEqual(outputs, [10, 20])
        self.assertTrue(receipt.exact)
        self.assertFalse(receipt.meta["candidate_exact"])
        self.assertEqual(len(receipt.meta["failure_events"]), 2)
        self.assertEqual(receipt.fallback_units, 2)
        self.assertLessEqual(receipt.speedup_x, 1.0)

    def test_failed_final_gate_charges_authoritative_fallback(self):
        units = [cx.SpecUnit(f"u{i}", "number", i) for i in range(3)]

        def baseline(u):
            time.sleep(0.002)
            return u.payload * 10

        engine = cx.SpeculativeEngine(
            branch_id="batch-gate", modality="number",
            draft=lambda u: cx.DraftProposal(u, u.payload * 10),
            verify=lambda p: cx.Verification(True, p.draft),
            repair=lambda p, _v: cx.RepairResult(p.draft),
            baseline=baseline,
            quality_gate=lambda _receipt: False,
        )
        outputs, receipt = engine.run(units)
        self.assertEqual(outputs, [0, 10, 20])
        self.assertFalse(receipt.quality_gate)
        self.assertEqual(receipt.fallback_units, 3)
        self.assertEqual(receipt.meta["final_gate_fallback_units"], 3)
        self.assertGreaterEqual(receipt.fallback_s, receipt.baseline_s)
        self.assertLessEqual(receipt.speedup_x, 1.0)

    def test_reused_baseline_does_not_hide_policy_overhead(self):
        unit = cx.SpecUnit("u", "number", 3)

        def baseline(u):
            time.sleep(0.006)
            return u.payload * 10

        def should_speculate(_u):
            time.sleep(0.004)
            return False

        engine = cx.SpeculativeEngine(
            branch_id="overhead", modality="number",
            draft=lambda u: cx.DraftProposal(u, u.payload),
            verify=lambda p: cx.Verification(True, p.draft),
            repair=lambda p, _v: cx.RepairResult(p.draft),
            baseline=baseline, should_speculate=should_speculate,
        )
        outputs, receipt = engine.run([unit])
        self.assertEqual(outputs, [30])
        self.assertGreater(receipt.overhead_s, 0.003)
        self.assertLess(receipt.speedup_x, 0.8)

    def test_modality_mismatch_and_unit_cap_rejected(self):
        engine = cx.SpeculativeEngine(
            branch_id="bounds", modality="number", max_units=1,
            draft=lambda u: cx.DraftProposal(u, u.payload),
            verify=lambda p: cx.Verification(True, p.draft),
            repair=lambda p, _v: cx.RepairResult(p.draft),
            baseline=lambda u: u.payload,
        )
        with self.assertRaises(ValueError):
            engine.run([cx.SpecUnit("u", "wrong", 1)])
        with self.assertRaises(ValueError):
            engine.run([cx.SpecUnit("u0", "number", 1), cx.SpecUnit("u1", "number", 2)])
        with self.assertRaises(ValueError):
            engine.run([])
        with self.assertRaises(ValueError):
            cx.SpecUnit("u", "bad lane", 1)
        with self.assertRaises(ValueError):
            cx.SpeculativeEngine(
                branch_id="🚀" * 256, modality="number",
                draft=lambda u: cx.DraftProposal(u, u.payload),
                verify=lambda p: cx.Verification(True, p.draft),
                repair=lambda p, v: cx.RepairResult(v.truth),
                baseline=lambda u: u.payload,
            )

    def test_duplicate_ids_fail_before_any_callback_runs(self):
        callback_calls = 0

        def baseline(unit):
            nonlocal callback_calls
            callback_calls += 1
            return unit.payload

        engine = cx.SpeculativeEngine(
            branch_id="unique-ledger", modality="number",
            draft=lambda u: cx.DraftProposal(u, u.payload),
            verify=lambda p: cx.Verification(True, p.draft),
            repair=lambda p, _v: cx.RepairResult(p.draft),
            baseline=baseline,
        )
        with self.assertRaisesRegex(ValueError, "duplicate unit_id"):
            engine.run([
                cx.SpecUnit("same", "number", 1),
                cx.SpecUnit("same", "number", 2),
            ])
        self.assertEqual(callback_calls, 0)

    def test_forged_same_id_proposal_cannot_rebind_production_work(self):
        original = cx.SpecUnit("u", "number", 7)

        def draft(_unit):
            forged = cx.SpecUnit("u", "number", 999)
            return cx.DraftProposal(forged, 9990)

        engine = cx.SpeculativeEngine(
            branch_id="binding", modality="number", draft=draft,
            verify=lambda p: cx.Verification(True, p.unit.payload * 10),
            repair=lambda p, v: cx.RepairResult(v.truth),
            baseline=lambda u: u.payload * 10,
        )
        outputs, receipt = engine.run([original], measure_baseline=False)
        self.assertEqual(outputs, [70])
        self.assertFalse(receipt.quality_gate)
        self.assertEqual(receipt.meta["disposition"], "fallback")
        self.assertIn(
            "exact input SpecUnit", receipt.meta["failure_events"][0]["error"]
        )

    def test_metadata_rejects_ambiguous_nonfinite_deep_and_oversized_json(self):
        with self.assertRaisesRegex(ValueError, "keys must be strings"):
            cx.SpecUnit("u", "number", 1, {1: "integer", "1": "string"})
        with self.assertRaisesRegex(ValueError, "finite"):
            cx.SpecUnit("u", "number", 1, {"bad": float("nan")})

        nested = {}
        for _ in range(cx.MAX_META_JSON_DEPTH):
            nested = {"child": nested}
        with self.assertRaisesRegex(ValueError, "nesting"):
            cx.SpecUnit("u", "number", 1, nested)

        with self.assertRaisesRegex(ValueError, "limit"):
            cx.SpecUnit(
                "u", "number", 1,
                {"blob": "x" * cx.MAX_META_JSON_BYTES},
            )

        # Reusing one object at a shallow and a too-deep location must not let a
        # global visited set hide the deeper occurrence.
        shared = {"value": 1}
        deep_alias = shared
        for _ in range(cx.MAX_META_JSON_DEPTH):
            deep_alias = [deep_alias]
        with self.assertRaisesRegex(ValueError, "nesting"):
            cx.SpecUnit("u", "number", 1, {"shallow": shared, "deep": deep_alias})

    def test_receipt_wire_revalidates_mutated_metadata(self):
        receipt = cx.SpecReceipt(
            branch_id="meta", modality="number", units=1,
            accepted_units=1, repaired_units=0, rejected_units=0,
            draft_s=0.1, verify_s=0.1, repair_s=0.0,
            baseline_s=1.0, speculative_s=0.2, speedup_x=5.0,
            exact=True, quality_gate=True, meta={"ok": True},
        )
        receipt.meta["later"] = float("inf")
        with self.assertRaisesRegex(ValueError, "finite"):
            receipt.to_dict()

    def test_unprintable_callback_exception_still_falls_back(self):
        class Unprintable(RuntimeError):
            def __str__(self):
                raise RuntimeError("secondary formatting failure")

        unit = cx.SpecUnit("u", "number", 3)
        engine = cx.SpeculativeEngine(
            branch_id="safe-error", modality="number",
            draft=lambda _u: (_ for _ in ()).throw(Unprintable()),
            verify=lambda p: cx.Verification(True, p.draft),
            repair=lambda p, v: cx.RepairResult(v.truth),
            baseline=lambda u: u.payload * 10,
        )
        outputs, receipt = engine.run([unit], measure_baseline=False)
        self.assertEqual(outputs, [30])
        self.assertFalse(receipt.quality_gate)
        self.assertIn("unprintable exception", receipt.meta["failure_events"][0]["error"])

    def test_quality_gate_failure_cannot_be_masked_by_reserved_meta_shape(self):
        unit = cx.SpecUnit("u", "number", 1)
        engine = cx.SpeculativeEngine(
            branch_id="gate-meta", modality="number",
            draft=lambda u: cx.DraftProposal(u, 10),
            verify=lambda p: cx.Verification(True, p.draft),
            repair=lambda p, v: cx.RepairResult(v.truth),
            baseline=lambda _u: 10,
            quality_gate=lambda _receipt: (_ for _ in ()).throw(RuntimeError("gate failed")),
        )
        outputs, receipt = engine.run(
            [unit], meta={"failure_events": "hostile non-list"}
        )
        self.assertEqual(outputs, [10])
        self.assertFalse(receipt.quality_gate)
        self.assertIsInstance(receipt.meta["failure_events"], list)
        self.assertEqual(receipt.meta["failure_events"][-1]["phase"], "quality_gate")

    def test_receipt_rejects_contradictory_counts(self):
        base = dict(
            branch_id="counts", modality="token", units=4, accepted_units=2,
            repaired_units=1, rejected_units=2, attempted_units=4,
            draft_s=0.1, verify_s=0.1, repair_s=0.1, baseline_s=0.6,
            speculative_s=0.3, speedup_x=2.0, exact=True, quality_gate=True,
        )
        cx.SpecReceipt(**base)
        with self.assertRaises(ValueError):
            cx.SpecReceipt(**{**base, "rejected_units": 1})
        with self.assertRaises(ValueError):
            cx.SpecReceipt(**{**base, "repaired_units": 3})
        with self.assertRaises(ValueError):
            cx.SpecReceipt(**{**base, "attempted_units": 5, "rejected_units": 3})
        with self.assertRaises(ValueError):
            cx.SpecReceipt(**{
                **base,
                "draft_s": 3593.0,
                "verify_s": 0.2,
                "repair_s": 0.3,
                "overhead_s": 0.1,
                "speculative_s": 3600.0,
                "baseline_s": 7200.0,
                "speedup_x": 2.0,
            })

    def test_gate_callbacks_require_real_bools(self):
        unit = cx.SpecUnit("u", "number", 1)
        common = dict(
            branch_id="strict-bool", modality="number",
            draft=lambda u: cx.DraftProposal(u, 10),
            verify=lambda p: cx.Verification(True, p.draft),
            repair=lambda p, _v: cx.RepairResult(p.draft),
            baseline=lambda _u: 10,
        )
        _, equal_receipt = cx.SpeculativeEngine(
            **common, equal=lambda _a, _b: "true",
        ).run([unit])
        self.assertFalse(equal_receipt.quality_gate)
        self.assertFalse(equal_receipt.meta["candidate_exact"])

        _, policy_receipt = cx.SpeculativeEngine(
            **common, should_speculate=lambda _u: "false",
        ).run([unit])
        self.assertFalse(policy_receipt.quality_gate)

        _, batch_receipt = cx.SpeculativeEngine(
            **common, quality_gate=lambda _r: "true",
        ).run([unit])
        self.assertFalse(batch_receipt.quality_gate)

    def test_counterfactual_baseline_never_steers_successful_delivery(self):
        """A bad denominator removes the speed claim; verifier truth still owns delivery."""
        unit = cx.SpecUnit("u", "number", 1)
        engine = cx.SpeculativeEngine(
            branch_id="counterfactual", modality="number",
            draft=lambda u: cx.DraftProposal(u, 10),
            verify=lambda p: cx.Verification(True, 10),
            repair=lambda p, v: cx.RepairResult(v.truth),
            baseline=lambda _u: 999,
        )
        outputs, receipt = engine.run([unit])
        self.assertEqual(outputs, [10])
        self.assertTrue(receipt.quality_gate)
        self.assertEqual(receipt.baseline_source, "absent")
        self.assertEqual(receipt.baseline_s, 0.0)
        self.assertEqual(receipt.speedup_x, 0.0)
        self.assertIsNone(receipt.to_dict()["speedup_x"])
        self.assertFalse(receipt.meta["baseline_comparable"])

    def test_slow_baseline_is_counterfactual_on_verified_success(self):
        unit = cx.SpecUnit("u", "number", 1)

        def baseline(_u):
            time.sleep(0.02)
            return 10

        engine = cx.SpeculativeEngine(
            branch_id="counterfactual-timing", modality="number",
            draft=lambda u: cx.DraftProposal(u, 10),
            verify=lambda p: cx.Verification(True, 10),
            repair=lambda p, v: cx.RepairResult(v.truth),
            baseline=baseline,
        )
        outputs, receipt = engine.run([unit])
        self.assertEqual(outputs, [10])
        self.assertGreater(receipt.baseline_s, 0.015)
        self.assertLess(receipt.speculative_s, 0.01)
        self.assertTrue(receipt.meta["baseline_comparable"])

    def test_custom_fallback_charges_baseline_that_authorizes_it(self):
        unit = cx.SpecUnit("u", "number", 1)

        def baseline(_u):
            time.sleep(0.01)
            return 10

        engine = cx.SpeculativeEngine(
            branch_id="charged-fallback", modality="number",
            draft=lambda u: cx.DraftProposal(u, 10),
            verify=lambda p: cx.Verification(True, 10),
            repair=lambda p, v: cx.RepairResult(v.truth),
            baseline=baseline,
            should_speculate=lambda _u: False,
            fallback=lambda _u: 10,
        )
        outputs, receipt = engine.run([unit])
        self.assertEqual(outputs, [10])
        self.assertGreaterEqual(receipt.fallback_s, receipt.baseline_s)
        self.assertLessEqual(receipt.speedup_x, 1.0)

    def test_production_mode_skips_counterfactual_baseline_on_success(self):
        calls = 0

        def baseline(u):
            nonlocal calls
            calls += 1
            return u.payload * 10

        engine = cx.SpeculativeEngine(
            branch_id="production", modality="number",
            draft=lambda u: cx.DraftProposal(u, u.payload * 10),
            verify=lambda p: cx.Verification(True, p.draft),
            repair=lambda p, v: cx.RepairResult(v.truth),
            baseline=baseline,
        )
        outputs, receipt = engine.run(
            [cx.SpecUnit("u", "number", 2)], measure_baseline=False
        )
        self.assertEqual(outputs, [20])
        self.assertEqual(calls, 0)
        self.assertEqual(receipt.baseline_source, "absent")
        self.assertEqual(receipt.baseline_s, 0.0)
        self.assertIsNone(receipt.to_dict()["speedup_x"])
        self.assertFalse(receipt.meta["benchmark_mode"])

    def test_production_mode_lazily_runs_and_charges_authoritative_fallback(self):
        calls = 0

        def baseline(u):
            nonlocal calls
            calls += 1
            time.sleep(0.005)
            return u.payload * 10

        engine = cx.SpeculativeEngine(
            branch_id="production-fallback", modality="number",
            draft=lambda u: cx.DraftProposal(u, -1),
            verify=lambda p: cx.Verification(True, p.unit.payload * 10),
            repair=lambda p, v: cx.RepairResult(v.truth),
            baseline=baseline,
        )
        outputs, receipt = engine.run(
            [cx.SpecUnit("u", "number", 2)], measure_baseline=False
        )
        self.assertEqual(outputs, [20])
        self.assertEqual(calls, 1)
        self.assertEqual(receipt.fallback_units, 1)
        self.assertGreater(receipt.fallback_s, 0.004)
        self.assertEqual(receipt.baseline_source, "absent")
        self.assertEqual(receipt.speedup_x, 0.0)

    def test_production_batch_gate_lazily_falls_back_every_unit(self):
        calls = 0

        def baseline(u):
            nonlocal calls
            calls += 1
            return u.payload

        engine = cx.SpeculativeEngine(
            branch_id="production-batch-gate", modality="number",
            draft=lambda u: cx.DraftProposal(u, u.payload),
            verify=lambda p: cx.Verification(True, p.draft),
            repair=lambda p, v: cx.RepairResult(v.truth),
            baseline=baseline,
            quality_gate=lambda _receipt: False,
        )
        outputs, receipt = engine.run(
            [cx.SpecUnit("u0", "number", 1), cx.SpecUnit("u1", "number", 2)],
            measure_baseline=False,
        )
        self.assertEqual(outputs, [1, 2])
        self.assertEqual(calls, 2)
        self.assertEqual(receipt.fallback_units, 2)
        self.assertEqual(receipt.baseline_source, "absent")


if __name__ == "__main__":
    unittest.main()
