#!/usr/bin/env python3
"""Dependency-free tests for the local vLLM/spec-decode lab lane."""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_local_vllm_lab as lab  # noqa: E402


class RequestContractTests(unittest.TestCase):
    def test_request_exactly_matches_rust_adapter_contract(self) -> None:
        request = lab.completion_request("model", ["a", "b"], 16)
        self.assertEqual(
            request,
            {
                "model": "model",
                "prompt": ["a", "b"],
                "max_tokens": 16,
                "temperature": 0.0,
                "top_p": 1.0,
                "seed": 0,
                "n": 1,
                "logprobs": 0,
            },
        )

    def test_non_greedy_and_unknown_fields_fail_closed(self) -> None:
        request = lab.completion_request("model", ["a"], 4)
        request["temperature"] = 0.5
        with self.assertRaisesRegex(lab.LabError, "temperature"):
            lab._validate_request_contract(request)
        request = lab.completion_request("model", ["a"], 4)
        request["best_of"] = 2
        with self.assertRaisesRegex(lab.LabError, "fields mismatch"):
            lab._validate_request_contract(request)


class MockRoundTripTests(unittest.TestCase):
    def test_mock_round_trip_sorts_choices_and_uses_real_token_list(self) -> None:
        request = lab.completion_request("mock", ["one", "two"], 12)
        with lab.EmbeddedMock() as server:
            first = lab.request_completions(
                server.endpoint, request, timeout_s=3, api_key=None
            )
            second = lab.request_completions(
                server.endpoint, request, timeout_s=3, api_key=None
            )
        self.assertTrue(first.response_was_out_of_order)
        self.assertEqual([row.index for row in first.choices], [0, 1])
        self.assertEqual([row.token_count for row in first.choices], [12, 12])
        self.assertEqual(
            {row.token_source for row in first.choices}, {"logprobs.tokens"}
        )
        self.assertEqual(first.output_sha256, second.output_sha256)

    def test_mock_rejects_an_adapter_contract_drift(self) -> None:
        request = lab.completion_request("mock", ["one"], 4)
        request["temperature"] = 0.1
        with lab.EmbeddedMock() as server:
            raw = urllib.request.Request(
                server.endpoint + "/v1/completions",
                data=lab.canonical_json_bytes(request),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as context:
                urllib.request.urlopen(raw, timeout=3)
        self.assertEqual(context.exception.code, 400)
        context.exception.close()

    def test_single_choice_uses_authoritative_aggregate_and_keeps_policy_bytes(self) -> None:
        choices, reordered = lab.map_completion_response(
            {
                "choices": [{"index": 0, "text": "hello  world", "logprobs": None}],
                "usage": {"completion_tokens": 7},
            },
            1,
        )
        self.assertFalse(reordered)
        self.assertEqual(choices[0].token_count, 7)
        self.assertEqual(choices[0].token_source, "usage.completion_tokens")
        self.assertEqual(len(choices[0].policy_tokens), len(b"hello  world"))

    def test_batch_without_per_choice_tokens_is_rejected(self) -> None:
        with self.assertRaisesRegex(lab.LabError, "no authoritative per-choice"):
            lab.map_completion_response(
                {
                    "choices": [
                        {"index": 0, "text": "one"},
                        {"index": 1, "text": "two words"},
                    ],
                    "usage": {"completion_tokens": 9},
                },
                2,
            )

    def test_aggregate_and_per_choice_disagreement_is_rejected(self) -> None:
        with self.assertRaisesRegex(lab.LabError, "metadata disagrees"):
            lab.map_completion_response(
                {
                    "choices": [
                        {
                            "index": 0,
                            "text": "one",
                            "logprobs": {"tokens": ["one"]},
                        }
                    ],
                    "usage": {"completion_tokens": 2},
                },
                1,
            )

    def test_duplicate_choice_index_is_rejected(self) -> None:
        with self.assertRaisesRegex(lab.LabError, "duplicate choice index"):
            lab.map_completion_response(
                {
                    "choices": [
                        {"index": 0, "text": "a", "logprobs": {"tokens": ["a"]}},
                        {"index": 0, "text": "b", "logprobs": {"tokens": ["b"]}},
                    ],
                    "usage": {"completion_tokens": 2},
                },
                2,
            )


class PolicyReplayTests(unittest.TestCase):
    def test_replay_is_exact_and_cannot_claim_speedup(self) -> None:
        traces = [["a", "b", "c", "a", "b", "c"] * 8]
        result = lab.replay_policy(
            traces,
            width=4,
            context=2,
            prefix_fraction=0.25,
            evidence="imported",
            source_digest="a" * 64,
        )
        receipt = result["receipt"]
        self.assertEqual(result["status"], "ok")
        self.assertTrue(receipt["exact"])
        self.assertEqual(receipt["baseline_source"], "absent")
        self.assertIsNone(receipt["speedup_x"])
        self.assertEqual(result["branch_action"], "park")
        self.assertGreater(receipt["meta"]["token_acceptance_fraction"], 0.5)

    def test_short_trace_is_honestly_unavailable(self) -> None:
        result = lab.replay_policy(
            [["x"]],
            width=4,
            context=4,
            prefix_fraction=0.25,
            evidence="synthetic",
            source_digest="b" * 64,
        )
        self.assertEqual(result["status"], "insufficient_trace")
        self.assertNotIn("receipt", result)


class MetalDetectionTests(unittest.TestCase):
    def test_missing_install_is_reported_without_installing(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            lab.shutil, "which", return_value=None
        ), mock.patch.object(lab.importlib.metadata, "version", side_effect=lab.importlib.metadata.PackageNotFoundError):
            result = lab.inspect_vllm_metal(
                environ={}, home=Path(directory), workspace_parent=Path(directory)
            )
        self.assertIn(result["status"], {"not_installed", "unsupported_host"})
        self.assertFalse(result["install_performed"])
        self.assertFalse(result["server_started"])
        self.assertIsNone(result["installation"]["runtime_identity_sha256"])

    def test_local_metal_target_is_always_non_cuda(self) -> None:
        parser = lab.build_parser()
        args = parser.parse_args(
            [
                "run",
                "--target",
                "local-vllm-metal",
                "--endpoint",
                "http://127.0.0.1:8000",
                "--model",
                "model",
                "--runtime-id",
                "vllm-metal-test-build",
                "--variant",
                "ngram",
                "--no-ledger",
            ]
        )
        with mock.patch.object(
            lab,
            "inspect_vllm_metal",
            return_value={
                "installation": {"runtime_identity_sha256": None},
                "status": "not_installed",
            },
        ):
            target = lab.resolve_target(args, {"hardware_fingerprint_sha256": "c" * 64})
        self.assertEqual(target["accelerator_class"], "metal")
        self.assertFalse(target["cuda"])
        self.assertTrue(target["non_cuda"])
        self.assertEqual(target["benchmark_domain"], "local_metal_endpoint_e2e")
        self.assertFalse(target["runtime_configuration_attested"])


class TargetSafetyTests(unittest.TestCase):
    def _parse(self, *extra: str) -> argparse.Namespace:
        return lab.build_parser().parse_args(["run", *extra])

    def test_remote_cuda_requires_valid_runtime_lock(self) -> None:
        args = self._parse(
            "--target",
            "remote-vllm-cuda",
            "--endpoint",
            "https://example.invalid",
            "--no-ledger",
        )
        with self.assertRaisesRegex(lab.LabError, "requires --runtime-lock"):
            lab.resolve_target(args, {"hardware_fingerprint_sha256": "d" * 64})

    def test_checked_in_placeholder_lock_is_rejected(self) -> None:
        args = self._parse(
            "--target",
            "remote-vllm-cuda",
            "--endpoint",
            "https://example.invalid",
            "--runtime-lock",
            str(lab.VLLM_DIR / "v0.24.0-candidate.template.json"),
            "--no-ledger",
        )
        with self.assertRaisesRegex(lab.LabError, "invalid vLLM runtime lock"):
            lab.resolve_target(args, {"hardware_fingerprint_sha256": "e" * 64})

    def test_local_target_refuses_non_loopback_endpoint(self) -> None:
        args = self._parse(
            "--target",
            "local-vllm-metal",
            "--endpoint",
            "https://example.com",
            "--model",
            "model",
            "--no-ledger",
        )
        with self.assertRaisesRegex(lab.LabError, "must be loopback"):
            lab.resolve_target(args, {"hardware_fingerprint_sha256": "f" * 64})

    def test_non_loopback_plain_http_is_fail_closed(self) -> None:
        with self.assertRaisesRegex(lab.LabError, "allow-insecure-http"):
            lab._safe_endpoint(
                "http://example.com:8000", local_only=False, allow_insecure_http=False
            )


class CaptureTests(unittest.TestCase):
    def _mock_args(self, *extra: str) -> argparse.Namespace:
        args = lab.build_parser().parse_args(
            [
                "run",
                "--target",
                "embedded-mock",
                "--requests",
                "3",
                "--concurrency",
                "2",
                "--max-tokens",
                "16",
                "--run-label",
                "mock-baseline",
                "--comparison-group",
                "unit-matrix",
                "--no-ledger",
                *extra,
            ]
        )
        lab._validate_run_args(args)
        return args

    def test_capture_has_business_and_proof_bridge_fields_without_secret(self) -> None:
        args = self._mock_args("--hourly-cost-usd", "0.75")
        with mock.patch.dict(os.environ, {"CX_VLLM_API_KEY": "do-not-record-this"}):
            with lab.EmbeddedMock() as server:
                record = lab.build_capture(args, endpoint_override=server.endpoint)
        self.assertEqual(record["schema_version"], 1)
        self.assertEqual(record["record_kind"], "cx_vllm_spec_lab_capture")
        self.assertEqual(record["sampling"]["concurrency"], 2)
        self.assertGreater(record["measurement"]["measured_wall_s"], 0)
        self.assertGreater(record["measurement"]["output_tokens_per_second"], 0)
        self.assertIn("p95", record["measurement"]["latency_ms"])
        self.assertTrue(record["measurement"]["repeat_output_stable"])
        self.assertTrue(record["measurement"]["response_reordering_exercised"])
        self.assertFalse(record["provenance"]["cuda"])
        self.assertTrue(record["provenance"]["non_cuda"])
        self.assertEqual(record["pricing"]["hourly_cost"], 0.75)
        self.assertEqual(
            record["pricing"]["source"], "operator_supplied_cli"
        )
        self.assertEqual(
            record["performance_proof_bridge"]["status"],
            "lab_capture_only_not_performance_proof_observation",
        )
        self.assertNotIn("do-not-record-this", json.dumps(record))

    def test_capture_can_be_written_and_loaded(self) -> None:
        args = self._mock_args()
        with lab.EmbeddedMock() as server:
            record = lab.build_capture(args, endpoint_override=server.endpoint)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            lab.write_artifact(path, record)
            loaded = lab.load_capture(str(path))
        self.assertEqual(loaded["workload_sha256"], record["workload_sha256"])

    def test_malformed_capture_fails_without_comparison_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "record_kind": "cx_vllm_spec_lab_capture",
                        "status": "ok",
                    }
                )
            )
            with self.assertRaisesRegex(lab.LabError, "missing fields"):
                lab.load_capture(str(path))


class ComparisonTests(unittest.TestCase):
    def _capture_pair(self) -> tuple[dict, dict]:
        args = lab.build_parser().parse_args(
            [
                "run",
                "--target",
                "embedded-mock",
                "--requests",
                "2",
                "--max-tokens",
                "12",
                "--run-label",
                "baseline",
                "--comparison-group",
                "baseline-ngram-matrix",
                "--no-ledger",
            ]
        )
        lab._validate_run_args(args)
        with lab.EmbeddedMock() as server:
            baseline = lab.build_capture(args, endpoint_override=server.endpoint)
        # Recast the real, internally consistent capture as two local-Metal
        # fixtures. The comparison logic concerns identity/workload gates, not
        # whether this unit test has a model server installed.
        baseline["target"].update(
            {
                "class": "local-openai-compatible",
                "engine": "cx-candle-metal",
                "variant": "baseline",
                "accelerator_class": "metal",
                "execution_site": "local_process",
                "runtime_identity_sha256": "1" * 64,
            }
        )
        model_identity = {
            "revision": "test-revision",
            "weights_sha256": "3" * 64,
            "tokenizer_sha256": "4" * 64,
            "precision_id": "q4-test",
            "complete": True,
            "identity_source": "operator_supplied",
        }
        baseline["target"]["model_identity"] = model_identity
        baseline["workload"]["model_identity"] = model_identity
        baseline["workload_sha256"] = lab.sha256_json(baseline["workload"])
        baseline["provenance"].update({"non_cuda": True, "cuda": False})
        candidate = copy.deepcopy(baseline)
        candidate["run_label"] = "ngram"
        candidate["target"]["engine"] = "vllm-metal"
        candidate["target"]["variant"] = "ngram"
        candidate["target"]["runtime_identity_sha256"] = "2" * 64
        return baseline, candidate

    def test_baseline_vs_ngram_same_metal_workload_is_comparable(self) -> None:
        baseline, candidate = self._capture_pair()
        result = lab.compare_captures(baseline, candidate)
        self.assertTrue(result["comparability"]["lab_comparable"])
        self.assertTrue(result["comparability"]["same_non_cuda_substrate"])
        self.assertEqual(result["correctness"]["exact_output_match_rate"], 1.0)
        self.assertTrue(result["descriptive_endpoint_ratio_valid"])
        self.assertFalse(result["speed_claim_valid"])
        self.assertFalse(
            result["comparability"]["canonical_performance_proof_comparable"]
        )

    def test_workload_change_blocks_ratio_claim(self) -> None:
        baseline, candidate = self._capture_pair()
        candidate["workload_sha256"] = "9" * 64
        result = lab.compare_captures(baseline, candidate)
        self.assertFalse(result["comparability"]["lab_comparable"])
        self.assertFalse(result["descriptive_endpoint_ratio_valid"])
        self.assertFalse(result["speed_claim_valid"])
        self.assertIn("workload digest differs", result["comparability"]["reasons"])

    def test_output_divergence_blocks_speed_claim(self) -> None:
        baseline, candidate = self._capture_pair()
        candidate["samples"][0]["output_sha256"] = "8" * 64
        result = lab.compare_captures(baseline, candidate)
        self.assertTrue(result["comparability"]["lab_comparable"])
        self.assertFalse(result["correctness"]["passed"])
        self.assertFalse(result["descriptive_endpoint_ratio_valid"])
        self.assertFalse(result["speed_claim_valid"])

    def test_unpinned_model_identity_blocks_comparability(self) -> None:
        baseline, candidate = self._capture_pair()
        baseline["workload"]["model_identity"]["complete"] = False
        candidate["workload"]["model_identity"]["complete"] = False
        digest = lab.sha256_json(baseline["workload"])
        baseline["workload_sha256"] = digest
        candidate["workload_sha256"] = digest
        result = lab.compare_captures(baseline, candidate)
        self.assertFalse(result["comparability"]["lab_comparable"])
        self.assertIn(
            "baseline model identity is incomplete",
            result["comparability"]["reasons"],
        )


if __name__ == "__main__":
    unittest.main()
