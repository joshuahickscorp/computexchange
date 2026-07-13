from __future__ import annotations

import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import run_customer_render_path_benchmark as model


def observed_fixture() -> dict:
    return {
        "basic_cycles_frame9": {
            "local_wall_s": 112.396726,
            "receipt_trust": "local_unattested",
            "scope": "fixture",
        },
        "exact_cache_frame9": {
            "artifact_bytes": 8310590,
            "artifact_sha256": "a" * 64,
            "local_median_s": 0.011900375,
            "local_p95_s_type7": 0.012294225,
            "local_slowest_s": 0.012490875,
            "local_trial_count": 9,
            "request_identity": "fixture-request-identity",
            "source_eligibility": {
                "artifact_verified": False,
                "billing_eligible": False,
                "preview_only": True,
                "production_ready": False,
            },
            "transport_authorized": True,
            "scope": "fixture",
        },
        "basic_cycles_frame11": {
            "local_wall_s": 117.277528,
            "receipt_trust": "local_unattested",
            "scope": "fixture",
        },
        "spatial75_two_render_frame11": {
            "customer_selectable_now": False,
            "experimental_preview_candidate": True,
            "local_median_s": 0.550618666,
            "local_p95_s_type7": 0.555222384,
            "local_slowest_s": 0.555684959,
            "local_trial_count": 7,
            "median_speedup_x": 212.992,
            "predeclared_quality_trial": 0,
            "quality_pass": True,
            "reference_free_pair_gate_pass": True,
            "request_identity": "fixture-spatial-request-identity",
            "scope": "fixture",
        },
        "spatial75_one_render_frame11": {
            "customer_selectable": False,
            "local_median_s": 0.332339667,
            "local_p95_s_type7": 0.349205434,
            "local_slowest_s": 0.349934834,
            "local_trial_count": 7,
            "median_speedup_x": 352.885,
            "quality_pass_count": 7,
            "reason": "gate omitted",
        },
        "temporal_shadow_only": {
            "arms": [],
            "customer_selectable": False,
            "experimental_only": True,
            "receipt_trust": "local_unattested",
        },
    }


class StrictInputTests(unittest.TestCase):
    def test_duplicate_json_key_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "duplicate.json"
            path.write_text('{"a":1,"a":2}')
            with self.assertRaisesRegex(model.CustomerPathModelError, "duplicate"):
                model.read_json_object(path)

    def test_symlink_evidence_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "target.json"
            target.write_text("{}")
            link = root / "link.json"
            link.symlink_to(target)
            with self.assertRaisesRegex(model.CustomerPathModelError, "non-symlink"):
                model.read_json_object(link)

    def test_nonfinite_json_constant_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "nonfinite.json"
            path.write_text('{"value":NaN}')
            with self.assertRaisesRegex(model.CustomerPathModelError, "non-finite"):
                model.read_json_object(path)

    def test_oversized_evidence_rejected_before_parse(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "oversized.json"
            with path.open("wb") as handle:
                handle.truncate(model.MAX_EVIDENCE_BYTES + 1)
            with self.assertRaisesRegex(model.CustomerPathModelError, "byte length"):
                model.read_json_object(path)

    def test_receipt_substitution_during_validation_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            baseline = root / "baseline.json"
            baseline_bytes = b'{"baseline":true}'
            baseline.write_bytes(baseline_bytes)
            cache = root / "cache.json"
            cache.write_text(
                json.dumps(
                    {
                        "artifacts": {
                            "source_performance_receipt": {
                                "bytes": len(baseline_bytes),
                                "path": baseline.name,
                                "sha256": model.hashlib.sha256(
                                    baseline_bytes
                                ).hexdigest(),
                            }
                        }
                    }
                )
            )
            spatial = root / "spatial.json"
            one = root / "one.json"
            temporal = root / "temporal.json"
            spatial.write_text('{"spatial":true}')
            one.write_text('{"one":true}')
            temporal.write_text('{"temporal":true}')

            def mutate_cache(*_args: object, **_kwargs: object) -> None:
                cache.write_text('{"substituted":true}')

            with (
                mock.patch.object(
                    model.cache_frontier,
                    "validate_screen_receipt",
                    side_effect=mutate_cache,
                ),
                mock.patch.object(model.spatial_frontier, "validate_receipt"),
                mock.patch.object(model.one_render_frontier, "validate_closed_report"),
                mock.patch.object(
                    model.temporal_frontier,
                    "validate_receipt_path",
                    return_value={"temporal": True},
                ),
            ):
                with self.assertRaisesRegex(
                    model.CustomerPathModelError, "changed after validation"
                ):
                    model.load_validated_receipts(
                        cache_receipt_path=cache,
                        spatial_receipt_path=spatial,
                        one_render_report_path=one,
                        temporal_receipt_path=temporal,
                    )

    def test_csv_parser_rejects_non_number(self) -> None:
        with self.assertRaisesRegex(model.CustomerPathModelError, "non-number"):
            model.parse_csv_numbers("0.5,nope", "rates")

    def test_nonfinite_and_duplicate_inputs_rejected(self) -> None:
        with self.assertRaisesRegex(model.CustomerPathModelError, "finite"):
            model.build_model(
                observed=observed_fixture(),
                provenance={},
                hit_rates=[math.nan],
            )
        with self.assertRaisesRegex(model.CustomerPathModelError, "unique"):
            model.build_model(
                observed=observed_fixture(),
                provenance={},
                hit_rates=[0.5, 0.5],
            )


class ExactRepeatModelTests(unittest.TestCase):
    def test_zero_overhead_thresholds_match_closed_form(self) -> None:
        expected = {
            2.0: 0.500052944758929,
            10.0: 0.9000953005660722,
            100.0: 0.9901048306226795,
            1000.0: 0.9991057836283402,
        }
        for target, want in expected.items():
            row = model.required_hit_rate(112.396726, 0.011900375, 0.0, target)
            self.assertTrue(row["reachable"])
            self.assertAlmostEqual(row["required_hit_rate"], want, places=14)

    def test_shared_overhead_caps_maximum_speedup(self) -> None:
        row = model.required_hit_rate(112.396726, 0.011900375, 0.2, 1000.0)
        self.assertFalse(row["reachable"])
        self.assertIsNone(row["required_hit_rate"])
        self.assertAlmostEqual(row["maximum_all_hit_speedup_x"], 531.366336657)

    def test_two_point_tail_stays_on_miss_until_quantile_coverage(self) -> None:
        hit = 0.011900375
        miss = 112.396726
        self.assertEqual(model.two_point_quantile(0.94, hit, miss, 0.95), miss)
        self.assertEqual(model.two_point_quantile(0.95, hit, miss, 0.95), hit)
        self.assertEqual(model.two_point_quantile(0.98, hit, miss, 0.99), miss)
        self.assertEqual(model.two_point_quantile(0.99, hit, miss, 0.99), hit)

    def test_cache_population_is_charged(self) -> None:
        self.assertEqual(
            model.minimum_population_for_speedup(
                112.396726, 0.011900375, 1000.0
            ),
            1119,
        )
        self.assertIsNone(
            model.minimum_population_for_speedup(
                112.396726, 0.011900375, 10000.0
            )
        )


class CustomerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.result = model.build_model(
            observed=observed_fixture(),
            provenance={"fixture": {"sha256": "b" * 64}},
        )

    def test_final_route_remains_basic_cycles(self) -> None:
        final = self.result["current_customer_policy"]["final_delivery"]
        self.assertEqual(final["route"], "basic_cycles")
        self.assertFalse(final["accelerated_lane_authorized_now"])

    def test_audit_arms_are_not_selectable(self) -> None:
        policy = self.result["current_customer_policy"]
        self.assertFalse(policy["one_render"]["customer_selectable"])
        self.assertFalse(policy["animation_temporal"]["customer_selectable"])

    def test_policy_replays_keep_final_and_failures_on_basic_cycles(self) -> None:
        replays = self.result["policy_replays"]
        self.assertEqual(
            replays["current_exact_preview"]["delivered_route"], "exact_cache"
        )
        self.assertEqual(
            replays["preview_cache_miss"]["delivered_route"], "spatial75"
        )
        self.assertEqual(
            replays["current_final_request"]["delivered_route"], "basic_cycles"
        )
        self.assertEqual(
            replays["preview_gate_failure"]["delivered_route"], "basic_cycles"
        )

    def test_cross_receipt_hybrid_is_never_headline_evidence(self) -> None:
        hybrid = self.result["models"]["illustrative_cache_then_spatial_preview"]
        self.assertFalse(hybrid["empirical_integrated_route"])
        self.assertFalse(hybrid["headline_eligible"])
        threshold = next(
            row for row in hybrid["target_thresholds"]
            if row["target_speedup_x"] == 1000.0
        )
        self.assertAlmostEqual(threshold["required_hit_rate"], 0.8043928436058986)

    def test_progressive_preview_quantifies_small_extra_compute(self) -> None:
        progressive = self.result["models"]["progressive_preview_then_full_cycles"]
        self.assertAlmostEqual(
            progressive["gated_preview_sequential_extra_compute_percent"],
            0.469501,
            places=6,
        )
        self.assertAlmostEqual(
            progressive["one_render_savings_vs_gated_preview_s"],
            0.218278999,
            places=9,
        )

    def test_payload_floor_is_not_labeled_end_to_end(self) -> None:
        payload = self.result["models"]["payload_serialization_floor"]
        row = next(item for item in payload["rows"] if item["link_mbps_decimal"] == 100.0)
        self.assertAlmostEqual(row["wire_seconds_at_line_rate"], 0.6648472)
        self.assertIn("RTT", payload["excludes"])

    def test_output_is_strict_canonical_json(self) -> None:
        encoded = model.canonical_json(self.result)
        self.assertEqual(json.loads(encoded)["kind"], model.KIND)
        self.assertNotIn("NaN", encoded)


if __name__ == "__main__":
    unittest.main()
