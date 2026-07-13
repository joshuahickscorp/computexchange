#!/usr/bin/env python3
"""Deterministic contract tests for scripts/performance_proof.py."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import performance_proof as proof  # noqa: E402


class PerformanceProofTest(unittest.TestCase):
    def setUp(self):
        self.baseline_manifest = proof.load_json(proof.DEFAULT_BASELINE_MANIFEST)
        self.candidate_manifest = proof.load_json(proof.DEFAULT_CANDIDATE_MANIFEST)
        self.baseline_observations = proof.load_json(proof.DEFAULT_BASELINE_OBSERVATIONS)
        self.pass_observations = proof.load_json(proof.DEFAULT_PASS_OBSERVATIONS)
        self.fault_observations = proof.load_json(proof.DEFAULT_FAULT_OBSERVATIONS)

    def test_schema_names_every_required_pinned_surface(self):
        schema = proof.load_json(proof.DEFAULT_SCHEMA)
        self.assertFalse(schema["additionalProperties"])
        workload = schema["$defs"]["workload"]["required"]
        self.assertEqual(
            set(workload), {"job_type", "model", "precision", "prompt_corpus", "output", "batch"}
        )
        self.assertIn("runtime", schema["required"])
        self.assertIn("hardware", schema["required"])
        self.assertIn("environment", schema["required"])
        self.assertIn("timing_scope", schema["required"])
        self.assertEqual(
            set(schema["$defs"]["output"]["required"]),
            {
                "requested_tokens_per_prompt",
                "include_prompt_tokens",
                "stop_conditions_sha256",
                "throughput_unit",
            },
        )
        self.assertFalse(schema["$defs"]["output"]["properties"]["include_prompt_tokens"]["const"])
        for field in (
            "provisioning_included",
            "queue_included",
            "transfer_in_included",
            "transfer_out_included",
        ):
            self.assertTrue(schema["$defs"]["timing_scope"]["properties"][field]["const"])
        self.assertTrue(
            schema["$defs"]["regression_policy"]["properties"]["require_cleanup"]["const"]
        )
        difference_enum = set(schema["properties"]["allowed_differences"]["items"]["enum"])
        self.assertEqual(difference_enum, proof.ALLOWED_DIFFERENCES)

    def test_valid_pair_is_deterministic_and_allows_declared_runtime_change(self):
        first = proof.compare_manifests(self.baseline_manifest, self.candidate_manifest)
        reordered = copy.deepcopy(self.candidate_manifest)
        reordered["allowed_differences"].reverse()
        second = proof.compare_manifests(self.baseline_manifest, reordered)
        self.assertEqual(first, second)
        self.assertEqual(first["comparison_scope"], "same_lane")
        self.assertEqual(first["changed_differences"], ["runtime.engine", "runtime.toolchain"])

    def test_cross_substrate_hardware_and_power_changes_are_explicitly_representable(self):
        baseline = copy.deepcopy(self.baseline_manifest)
        candidate = copy.deepcopy(self.candidate_manifest)
        paths = [
            "lane_id",
            "hardware.host_id_sha256",
            "hardware.sku",
            "hardware.accelerator_count",
            "hardware.accelerator_memory_gb",
            "hardware.system_memory_gb",
            "hardware.cpu",
            "hardware.os",
            "hardware.os_version",
            "hardware.driver",
            "hardware.firmware",
            "environment.power.source",
            "environment.power.mode",
            "environment.power.limit_watts",
            "environment.power.measurement_source",
            "environment.thermal.temperature_c",
            "environment.thermal.sensor",
            "environment.thermal.fan_policy",
            "environment.competing_load.process_snapshot_sha256",
            "environment.competing_load.cpu_utilization_pct",
            "environment.competing_load.gpu_utilization_pct",
            "environment.competing_load.memory_used_gb",
        ]
        baseline["allowed_differences"].extend(paths)
        candidate["allowed_differences"].extend(paths)
        candidate["lane_id"] = "synthetic_candle_cuda"
        candidate["hardware"] = {
            "host_id_sha256": "1212121212121212121212121212121212121212121212121212121212121212",
            "sku": "synthetic-nvidia-a100-sxm-80gb",
            "accelerator_count": 2,
            "accelerator_memory_gb": 160,
            "system_memory_gb": 256,
            "cpu": "synthetic-amd-epyc",
            "os": "synthetic-linux",
            "os_version": "fixture-2.0",
            "driver": "fixture-cuda-driver-2",
            "firmware": "fixture-gpu-firmware-2",
        }
        candidate["environment"]["power"] = {
            "source": "datacenter-pdu",
            "mode": "synthetic-fixed-power",
            "limit_watts": 800,
            "measurement_source": "synthetic-pdu-meter",
        }
        candidate["environment"]["thermal"].update(
            temperature_c=55, sensor="synthetic-nvidia-smi", fan_policy="synthetic-fixed"
        )
        candidate["environment"]["competing_load"].update(
            process_snapshot_sha256="1313131313131313131313131313131313131313131313131313131313131313",
            cpu_utilization_pct=3,
            gpu_utilization_pct=1,
            memory_used_gb=8,
        )
        result = proof.compare_manifests(baseline, candidate)
        self.assertEqual(result["comparison_scope"], "cross_substrate")
        self.assertEqual(result["candidate_lane_id"], "synthetic_candle_cuda")
        self.assertTrue(set(paths) <= set(result["changed_differences"]))

    def test_undeclared_hardware_change_is_incomparable(self):
        candidate = copy.deepcopy(self.candidate_manifest)
        candidate["hardware"]["sku"] = "different-sku"
        with self.assertRaisesRegex(proof.IncomparableManifestError, "hardware.sku"):
            proof.compare_manifests(self.baseline_manifest, candidate)

    def test_workload_and_timing_changes_can_never_be_declared_comparable(self):
        mutations = (
            (("workload", "model", "weights_sha256"), "0" * 64),
            (("workload", "model", "tokenizer_sha256"), "f" * 64),
            (("workload", "precision", "compute_dtype"), "bf16"),
            (("workload", "precision", "quantization", "bits"), 8),
            (("workload", "prompt_corpus", "sha256"), "e" * 64),
            (("workload", "output", "requested_tokens_per_prompt"), 32),
            (("workload", "batch", "batch_size"), 2),
            (("timing_scope", "clock"), "different-monotonic-clock"),
        )
        for path, value in mutations:
            with self.subTest(path=path):
                candidate = copy.deepcopy(self.candidate_manifest)
                cursor = candidate
                for part in path[:-1]:
                    cursor = cursor[part]
                cursor[path[-1]] = value
                with self.assertRaises(proof.ContractError):
                    proof.compare_manifests(self.baseline_manifest, candidate)

    def test_unknown_fields_placeholders_and_invalid_quantization_fail_closed(self):
        cases = []
        unknown = copy.deepcopy(self.baseline_manifest)
        unknown["wishful_claim"] = True
        cases.append(unknown)
        placeholder = copy.deepcopy(self.baseline_manifest)
        placeholder["runtime"]["engine"]["version"] = "latest"
        cases.append(placeholder)
        quant = copy.deepcopy(self.baseline_manifest)
        quant["workload"]["precision"]["quantization"]["bits"] = 0
        cases.append(quant)
        excluded_timing = copy.deepcopy(self.baseline_manifest)
        excluded_timing["timing_scope"]["queue_included"] = False
        cases.append(excluded_timing)
        for manifest in cases:
            with self.subTest(case=len(cases)):
                with self.assertRaises(proof.ContractError):
                    proof.validate_manifest(manifest)

    def test_observations_bind_manifest_hash_lane_environment_and_sample_count(self):
        mutations = (
            ("manifest_sha256", "0" * 64),
            ("lane_id", "wrong_lane"),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                observations = copy.deepcopy(self.baseline_observations)
                observations[field] = value
                with self.assertRaisesRegex(proof.ContractError, "does not bind"):
                    proof.validate_observations(observations, self.baseline_manifest)
        too_short = copy.deepcopy(self.baseline_observations)
        too_short["samples"].pop()
        with self.assertRaisesRegex(proof.ContractError, "exactly 20"):
            proof.validate_observations(too_short, self.baseline_manifest)
        wrong_thermal = copy.deepcopy(self.baseline_observations)
        wrong_thermal["samples"][0]["thermal_state"] = "serious"
        with self.assertRaisesRegex(proof.ContractError, "pinned manifest state"):
            proof.validate_observations(wrong_thermal, self.baseline_manifest)

    def test_throughput_units_are_derived_from_pinned_batch_and_output_shape(self):
        inflated = copy.deepcopy(self.pass_observations)
        inflated["samples"][0]["completed_units"] = 1_000_000_000
        with self.assertRaisesRegex(proof.ContractError, "batch_size × requested output tokens"):
            proof.validate_observations(inflated, self.candidate_manifest)

        wrong_batch = copy.deepcopy(self.pass_observations)
        wrong_batch["samples"][0]["batch_size"] = 3
        wrong_batch["samples"][0]["completed_units"] = 3 * 64
        with self.assertRaisesRegex(proof.ContractError, "pinned fixed batch"):
            proof.validate_observations(wrong_batch, self.candidate_manifest)

    def test_nearest_rank_p95_and_p99_are_deterministic(self):
        values = list(range(1, 101))
        self.assertEqual(proof.percentile(values, 95), 95)
        self.assertEqual(proof.percentile(values, 99), 99)

    def test_passing_synthetic_fixture_passes_without_claiming_physical_evidence(self):
        result = proof.evaluate_pair(
            self.baseline_manifest,
            self.candidate_manifest,
            self.baseline_observations,
            self.pass_observations,
        )
        self.assertEqual(result["status"], "PASS")
        self.assertFalse(result["physical_evidence"])
        self.assertEqual(result["performance_gate_status"], "NOT_PROVEN_BY_SYNTHETIC_FIXTURE")
        self.assertTrue(all(row["passed"] for row in result["assertions"]))

    def test_fault_fixture_detects_every_required_regression_and_fault_class(self):
        result = proof.evaluate_pair(
            self.baseline_manifest,
            self.candidate_manifest,
            self.baseline_observations,
            self.fault_observations,
        )
        failures = {row["id"] for row in result["assertions"] if not row["passed"]}
        expected = {
            "throughput_ratio",
            "p95_latency_ratio",
            "p99_latency_ratio",
            "candidate.oom_events",
            "candidate.restart_events",
            "candidate.disconnect_events",
            "candidate.corrupt_output_events",
            "candidate.cleanup_failures",
        }
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(expected <= failures)

    def test_each_fault_channel_is_evaluated_independently(self):
        for event_type in ("oom", "restart", "disconnect"):
            with self.subTest(event_type=event_type):
                candidate = copy.deepcopy(self.pass_observations)
                candidate["events"] = [
                    {"type": event_type, "sample_id": candidate["samples"][0]["sample_id"], "evidence_sha256": "c" * 64}
                ]
                result = proof.evaluate_pair(
                    self.baseline_manifest, self.candidate_manifest, self.baseline_observations, candidate
                )
                failed = {row["id"] for row in result["assertions"] if not row["passed"]}
                self.assertIn(f"candidate.{event_type}_events", failed)

    def test_generated_fixture_proof_is_deterministic_and_stale_check_does_not_rewrite(self):
        first = proof.build_fixture_proof()
        second = proof.build_fixture_proof()
        self.assertEqual(proof.pretty_json_bytes(first), proof.pretty_json_bytes(second))
        self.assertEqual(first["contract_status"], "PASS")
        self.assertEqual(first["actual_benchmark_gate_status"], "NOT_PROVEN")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "proof.json"
            self.assertTrue(proof.write_or_check(path, first, False))
            path.write_text("stale\n", encoding="utf-8")
            self.assertFalse(proof.write_or_check(path, first, True))
            self.assertEqual(path.read_text(encoding="utf-8"), "stale\n")


if __name__ == "__main__":
    unittest.main()
