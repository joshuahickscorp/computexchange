#!/usr/bin/env python3
"""Contract tests for the strict, non-composable 1,000x scorecard."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import score_1000x_lanes as score  # noqa: E402


DIGEST = "a" * 64


def lane(
    lane_id: str,
    *,
    baseline: float = 1000.0,
    candidate: float = 1.0,
    direction: str = "lower_is_better",
    quality_gate: str = "predeclared_independent",
    evidence_class: str = "physical_local_unattested",
    timing_scope: str = "integrated_wall",
    same_logical_work: bool = True,
    receipt: str | None = DIGEST,
    authorization: bool = False,
    measured: bool = True,
    guardrail: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "lane_id": lane_id,
        "display_name": lane_id,
        "modality": "render",
        "metric_kind": "fresh_render",
        "claim_axis": "fresh_frame_latency",
        "comparison_group": "fixture-group",
        "measurement": (
            {
                "direction": direction,
                "unit": "s/frame" if direction == "lower_is_better" else "tok/s",
                "statistic": "median",
                "baseline_value": baseline,
                "candidate_value": candidate,
                "same_logical_work": same_logical_work,
                "timing_scope": timing_scope,
                "baseline_source": "measured",
                "evidence_class": evidence_class,
                "trials": 7,
            }
            if measured
            else None
        ),
        "quality_gate": quality_gate if measured else "not_run",
        "authorization": {
            "artifact_verified": authorization,
            "customer_selectable": authorization,
            "publication_eligible": authorization,
            "production_ready": authorization,
            "billing_eligible": authorization,
        },
        "evidence": {
            "source": "docs/research/fixture.md",
            "receipt_sha256": receipt,
            "workload_sha256": DIGEST,
        },
        "guardrail": guardrail,
        "coverage": None,
        "lift": {"limiter": "fixture limiter", "next_experiment": "fixture experiment"},
    }


def scorecard(*lanes: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "standard_id": "cx-1000x-fixture",
        "as_of": "2026-07-14",
        "target_multiplier": 1000,
        "lanes": list(lanes),
    }


class Score1000xLanesTest(unittest.TestCase):
    def test_direct_authorized_lane_is_a_gold_standard(self) -> None:
        result = score.evaluate_scorecard(scorecard(lane("authorized", authorization=True)))
        row = result["lanes"][0]
        self.assertEqual(row["numeric_multiplier"], 1000.0)
        self.assertEqual(row["target_candidate_value"], 1.0)
        self.assertEqual(row["remaining_multiplier_to_target"], 1.0)
        self.assertEqual(row["performance_status"], "above_target")
        self.assertEqual(row["claim_status"], "production")
        self.assertIsNone(result["aggregate_multiplier"])

    def test_above_target_without_authorization_is_preserved_as_experimental(self) -> None:
        result = score.evaluate_scorecard(scorecard(lane("cache-hit")))
        row = result["lanes"][0]
        self.assertEqual(row["performance_status"], "above_target")
        self.assertEqual(row["claim_status"], "experimental")
        self.assertTrue(row["direct_evidence_eligible"])
        self.assertFalse(row["authorization_ready"])

    def test_subtarget_lane_reports_exact_remaining_lift(self) -> None:
        result = score.evaluate_scorecard(scorecard(lane("fresh", baseline=1000, candidate=5)))
        row = result["lanes"][0]
        self.assertEqual(row["numeric_multiplier"], 200.0)
        self.assertEqual(row["target_candidate_value"], 1.0)
        self.assertEqual(row["candidate_distance_to_target"], 4.0)
        self.assertEqual(row["remaining_multiplier_to_target"], 5.0)
        self.assertEqual(row["performance_status"], "lift_required")

    def test_higher_is_better_uses_throughput_formula(self) -> None:
        result = score.evaluate_scorecard(
            scorecard(lane("decode", baseline=200, candidate=400, direction="higher_is_better"))
        )
        row = result["lanes"][0]
        self.assertEqual(row["numeric_multiplier"], 2.0)
        self.assertEqual(row["target_candidate_value"], 200000.0)
        self.assertEqual(row["candidate_distance_to_target"], 199600.0)
        self.assertEqual(row["remaining_multiplier_to_target"], 500.0)

    def test_composed_posthoc_target_is_quarantined_not_achieved(self) -> None:
        result = score.evaluate_scorecard(
            scorecard(
                lane(
                    "temporal",
                    quality_gate="posthoc",
                    evidence_class="composed_estimate",
                    timing_scope="composed",
                    same_logical_work=False,
                )
            )
        )
        row = result["lanes"][0]
        self.assertEqual(row["numeric_multiplier"], 1000.0)
        self.assertEqual(row["performance_status"], "incomparable")
        self.assertEqual(row["claim_status"], "audit_only")

    def test_parity_failure_is_quarantined_even_below_target(self) -> None:
        result = score.evaluate_scorecard(
            scorecard(lane("parity", baseline=1000, candidate=500, quality_gate="parity_failed"))
        )
        row = result["lanes"][0]
        self.assertEqual(row["performance_status"], "incomparable")
        self.assertEqual(row["claim_status"], "quarantined")

    def test_missing_durable_receipt_is_not_direct_evidence(self) -> None:
        result = score.evaluate_scorecard(scorecard(lane("unbound", receipt=None)))
        row = result["lanes"][0]
        self.assertFalse(row["direct_evidence_eligible"])
        self.assertEqual(row["performance_status"], "incomparable")

    def test_null_measurement_remains_an_explicit_lift_lane(self) -> None:
        result = score.evaluate_scorecard(scorecard(lane("video", measured=False)))
        row = result["lanes"][0]
        self.assertEqual(row["performance_status"], "unmeasured")
        self.assertEqual(row["claim_status"], "unmeasured")
        self.assertIsNone(row["numeric_multiplier"])

    def test_cache_coverage_does_not_claim_portfolio_target_without_observation(self) -> None:
        exact = lane("exact")
        exact["metric_kind"] = "exact_reuse"
        exact["coverage"] = {
            "required_hit_rate": 0.999,
            "observed_hit_rate": None,
            "tail_multiplier": 1200,
            "tail_statistic": "slowest",
        }
        result = score.evaluate_scorecard(scorecard(exact))
        self.assertEqual(result["lanes"][0]["coverage_status"], "PORTFOLIO_NOT_EVALUABLE")

    def test_rejects_duplicate_unsafe_and_nonexact_inputs(self) -> None:
        with self.assertRaisesRegex(score.ScorecardError, "duplicate lane_id"):
            score.validate_scorecard(scorecard(lane("same"), lane("same")))

        invalid = scorecard(lane("zero"))
        invalid["lanes"][0]["measurement"]["baseline_value"] = 0  # type: ignore[index]
        with self.assertRaisesRegex(score.ScorecardError, "baseline_value"):
            score.validate_scorecard(invalid)

        invalid = scorecard(lane("bad-path"))
        invalid["lanes"][0]["evidence"]["source"] = "../outside.md"  # type: ignore[index]
        with self.assertRaisesRegex(score.ScorecardError, "relative repository path"):
            score.validate_scorecard(invalid)

    def test_cli_is_deterministic_and_preservation_gate_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scorecard.json"
            first = root / "first.json"
            second = root / "second.json"
            good = lane(
                "cache",
                guardrail={
                    "statistic": "slowest",
                    "observed_multiplier": 1100,
                    "minimum_multiplier": 1000,
                },
            )
            source.write_text(json.dumps(scorecard(lane("b"), good, lane("a"))), encoding="utf-8")
            self.assertEqual(
                score.main(["--scorecard", str(source), "--output", str(first), "--check-preserve", "cache"]),
                0,
            )
            self.assertEqual(score.main(["--scorecard", str(source), "--output", str(second)]), 0)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            result = json.loads(first.read_text(encoding="utf-8"))
            self.assertEqual([row["lane_id"] for row in result["lanes"]], ["a", "b", "cache"])

            bad = copy.deepcopy(good)
            bad["guardrail"]["observed_multiplier"] = 900  # type: ignore[index]
            source.write_text(json.dumps(scorecard(bad)), encoding="utf-8")
            self.assertEqual(
                score.main(["--scorecard", str(source), "--check-preserve", "cache"]), 2
            )

    def test_check_target_requires_a_direct_above_target_lane(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scorecard.json"
            output = root / "result.json"
            source.write_text(json.dumps(scorecard(lane("below", candidate=2))), encoding="utf-8")
            self.assertEqual(
                score.main(
                    ["--scorecard", str(source), "--output", str(output), "--check-target", "below"]
                ),
                3,
            )


if __name__ == "__main__":
    unittest.main()
