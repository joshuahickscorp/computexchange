#!/usr/bin/env python3
"""Focused contract tests for the non-production 20x resident-engine ledger."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bench_resident_engine as bench  # noqa: E402


def digest(char: str) -> str:
    return char * 64


def factors(*, correlated: bool, real: bool = False) -> list[dict[str, object]]:
    values = {
        "prefix_reuse": 2.0,
        "continuous_batching": 2.0,
        "kernel": 2.0,
        "speculation": 2.0,
        "residency": 2.0,
    }
    rows: list[dict[str, object]] = []
    for index, (lane, planned) in enumerate(values.items()):
        group = "decode" if correlated and lane in {"continuous_batching", "speculation"} else lane
        if real:
            evidence_kind = "real_wall_clock"
            observed: float | None = planned
            evidence = digest(chr(ord("a") + index))
        else:
            evidence_kind = "unproven"
            observed = None
            evidence = None
        rows.append(
            {
                "lane": lane,
                "planned_ratio": planned,
                "observed_ratio": observed,
                "evidence_kind": evidence_kind,
                "evidence_sha256": evidence,
                "correlation_group": group,
                "combination_evidence_sha256": None,
            }
        )
    return rows


def scenario(
    arm: str,
    *,
    timing_kind: str = "analytic_simulation",
    elapsed: float = 100.0,
    correlated: bool = True,
    ledger_real: bool = False,
) -> dict[str, object]:
    real = timing_kind == "real_wall_clock"
    return {
        "schema_version": 1,
        "scenario_id": f"resident-{arm}",
        "comparison_id": "resident-20x-fixture",
        "arm": arm,
        "provenance": {
            "engine_build_sha256": digest("1" if arm == "baseline" else "2"),
            "model_sha256": digest("3"),
            "tokenizer_sha256": digest("4"),
            "hardware_fingerprint_sha256": digest("5"),
            "workload_source_sha256": digest("6"),
            "trace_sha256": digest("7" if arm == "baseline" else "8"),
            "trace_kind": "runtime_trace" if real else "analytic_simulation",
            "timing_boundary": "end_to_end_verified_output",
        },
        "logical_work": {
            "corpus_sha256": digest("9"),
            "request_count": 4,
            "total_prompt_tokens": 80,
            "max_prompt_tokens": 32,
            "total_generated_tokens": 40,
            "output_sha256": digest("a"),
            "stop_conditions_sha256": digest("b"),
        },
        "scheduler": {
            "admissions": 4,
            "releases": 4,
            "dispatches": 5,
            "prefill_dispatches": 1,
            "decode_dispatches": 4,
            "batch_widths": [2, 4, 4, 3, 1],
            "max_active_slots": 4,
            "compactions": 2,
        },
        "kv": {
            "allocated_token_slots": 512,
            "peak_live_token_slots": 120,
            "live_token_slots_after": 0,
            "prefix_reused_prompt_tokens": 40,
            "kv_write_tokens": 100,
            "rollback_tokens": 4,
            "evictions": 0,
            "max_write_amplification": 2,
        },
        "speculation": {
            "enabled": True,
            "max_proposed_tokens_per_output": 4,
            "proposed_tokens": 48,
            "verified_tokens": 40,
            "accepted_tokens": 36,
            "rejected_tokens": 4,
            "verifier_dispatches": 12,
        },
        "measurements": {
            "kind": timing_kind,
            "simulated_elapsed_ms": None if real else elapsed,
            "wall_clock_samples_ms": [elapsed * 0.99, elapsed, elapsed * 1.01] if real else [],
            "wall_clock_observations_sha256": digest("c" if arm == "baseline" else "d") if real else None,
        },
        "target_ledger": None
        if arm == "baseline"
        else {"target_speedup": 20.0, "factors": factors(correlated=correlated, real=ledger_real)},
    }


class ResidentEngineBenchmarkTest(unittest.TestCase):
    def test_simulation_emits_bounded_metrics_and_refuses_correlated_plan_product(self):
        baseline = scenario("baseline", elapsed=200.0)
        candidate = scenario("candidate", elapsed=100.0, correlated=True)
        result = bench.evaluate_pair(baseline, candidate)

        self.assertEqual(result["production_status"], "NOT_A_PRODUCTION_CLAIM")
        self.assertEqual(result["evidence_label"], "SIMULATED_ANALYTIC_NOT_A_PRODUCTION_CLAIM")
        self.assertEqual(result["direct_speedup_ratio"], 2.0)
        self.assertFalse(result["direct_target_observed"])
        self.assertTrue(result["same_logical_work"])
        self.assertEqual(result["candidate"]["scheduler"]["slot_utilization"], 0.7)
        self.assertEqual(result["candidate"]["kv"]["prefix_reuse_fraction"], 0.5)
        self.assertEqual(result["candidate"]["speculation"]["accepted_fraction_of_verified"], 0.9)
        ledger = result["target_ledger"]
        self.assertTrue(ledger["factor_product_refused"])
        self.assertIsNone(ledger["declared_full_plan_product"])
        self.assertEqual(ledger["conservative_noncorrelated_plan_product"], 16.0)
        self.assertEqual(ledger["target_status"], "SIMULATED_ONLY_NOT_PROVEN")

    def test_real_wall_clock_observations_can_observe_direct_target_but_never_claim_production(self):
        baseline = scenario("baseline", timing_kind="real_wall_clock", elapsed=200.0)
        candidate = scenario(
            "candidate",
            timing_kind="real_wall_clock",
            elapsed=10.0,
            correlated=False,
            ledger_real=True,
        )
        result = bench.evaluate_pair(baseline, candidate)

        self.assertEqual(result["evidence_label"], "REAL_WALL_CLOCK_OBSERVATION_INGEST_NOT_A_PRODUCTION_CLAIM")
        self.assertEqual(result["direct_speedup_ratio"], 20.0)
        self.assertTrue(result["direct_target_observed"])
        self.assertEqual(
            result["target_ledger"]["target_status"],
            "DIRECT_REAL_WALL_CLOCK_TARGET_OBSERVED_NOT_A_PRODUCTION_CLAIM",
        )
        self.assertTrue(result["target_ledger"]["component_product_proven"])

    def test_rejects_incompatible_logical_work_and_mixed_timing(self):
        baseline = scenario("baseline")
        candidate = scenario("candidate")
        candidate["logical_work"]["output_sha256"] = digest("e")
        with self.assertRaisesRegex(bench.IncompatibleScenarioError, "logical_work"):
            bench.evaluate_pair(baseline, candidate)

        candidate = scenario("candidate", timing_kind="real_wall_clock", elapsed=10)
        with self.assertRaisesRegex(bench.IncompatibleScenarioError, "measurement kind"):
            bench.evaluate_pair(baseline, candidate)

    def test_rejects_unbounded_or_inconsistent_kv_and_spec_counters(self):
        invalid = scenario("candidate")
        invalid["kv"]["prefix_reused_prompt_tokens"] = 81
        with self.assertRaisesRegex(bench.ContractError, "prefix_reused_prompt_tokens"):
            bench.validate_scenario(invalid)

        invalid = scenario("candidate")
        invalid["speculation"]["accepted_tokens"] = 37
        with self.assertRaisesRegex(bench.ContractError, r"accepted_tokens \+ rejected_tokens"):
            bench.validate_scenario(invalid)

    def test_cli_loads_explicit_json_and_writes_deterministic_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline_path = root / "baseline.json"
            candidate_path = root / "candidate.json"
            artifact = root / "artifact.json"
            baseline_path.write_text(json.dumps(scenario("baseline")), encoding="utf-8")
            candidate_path.write_text(json.dumps(scenario("candidate")), encoding="utf-8")
            self.assertEqual(
                bench.main(["--baseline", str(baseline_path), "--candidate", str(candidate_path), "--artifact", str(artifact)]),
                0,
            )
            first = artifact.read_bytes()
            self.assertEqual(
                bench.main(["--baseline", str(baseline_path), "--candidate", str(candidate_path), "--artifact", str(artifact)]),
                0,
            )
            self.assertEqual(first, artifact.read_bytes())
            loaded = json.loads(first)
            self.assertEqual(loaded["artifact_kind"], "resident_engine_20x_benchmark_plan")


if __name__ == "__main__":
    unittest.main()
