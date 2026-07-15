#!/usr/bin/env python3
"""Focused contract tests for the immutable 50x inference-lane receipt."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import cx_inference_receipt_v1 as receipt  # noqa: E402
import score_1000x_lanes as score  # noqa: E402


def digest(character: str) -> str:
    return character * 64


def stage_times(total: float) -> dict[str, float]:
    return {stage: total if stage == "admission" else 0.0 for stage in receipt.STAGES}


def artifact_row(artifact_id: str, kind: str, path: str, data: bytes) -> dict[str, str]:
    return {
        "artifact_id": artifact_id,
        "kind": kind,
        "path": path,
        "sha256": receipt.sha256_bytes(data),
    }


def build_receipt(root: Path, *, lane: str = "exact_request_reuse") -> dict[str, object]:
    """Build one complete, ABBA-ordered receipt plus its retained artifacts."""

    artifacts: list[dict[str, str]] = []
    for index in range(16):
        path = root / "captures" / f"{index}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = f"capture-{index}".encode("utf-8")
        path.write_bytes(data)
        artifacts.append(
            artifact_row(f"capture-{index}", "sample_capture", f"captures/{index}.json", data)
        )
    for artifact_id, kind, filename in (
        ("parity", "parity_audit", "parity.json"),
        ("quality", "quality_audit", "quality.json"),
    ):
        data = f"{artifact_id}-audit".encode("utf-8")
        (root / filename).write_bytes(data)
        artifacts.append(artifact_row(artifact_id, kind, filename, data))

    workload: dict[str, object] = {
        "contract": "cx-openai-greedy-completions-v1",
        "request_set_sha256": digest("a"),
        "request_order_sha256": digest("b"),
        "request_count": 1,
        "max_output_tokens": 64,
        "sampling": {"temperature": 0, "top_p": 1, "seed": 0, "n": 1},
        "concurrency": 1,
        "customer_visible_stages": list(receipt.STAGES),
    }
    workload_sha = receipt.sha256_json(workload)
    samples: list[dict[str, object]] = []
    for index, arm in enumerate(receipt.ABBA * 4):
        elapsed = 100.0 if arm == "baseline" else 1.0
        samples.append(
            {
                "order": index,
                "sample_id": f"sample-{index}",
                "arm": arm,
                "workload_sha256": workload_sha,
                "request_count": 1,
                "elapsed_ms": elapsed,
                "stages_ms": stage_times(elapsed),
                "output_token_ids_sha256": digest("c"),
                "output_token_count": 64,
                "quality_sha256": digest("d"),
                "capture_artifact_id": f"capture-{index}",
                "status": "ok",
            }
        )
    coverage: dict[str, object] | None = {
        "coverage_workload_sha256": digest("e"),
        "observed_requests": 100,
        "eligible_requests": 80,
        "eligible_hits": 76,
        "eligible_request_fraction": 0.8,
        "eligible_hit_rate": 0.95,
        "required_eligible_hit_rate": 0.9,
    }
    reuse: dict[str, object] = {
        "scope": "exact_request",
        "baseline_cache_state": "disabled",
        "candidate_cache_state": "exact_response_cache_hit",
        "exact_response_reuse": True,
        "shared_prefix_tokens": 0,
        "coverage": coverage,
    }
    binding_lane = "inference.exact-request-reuse"
    if lane == "shared_prefix_reuse":
        reuse.update(
            {
                "scope": "shared_prefix",
                "candidate_cache_state": "prefix_kv_cache_hit",
                "exact_response_reuse": False,
                "shared_prefix_tokens": 512,
            }
        )
        binding_lane = "inference.shared-prefix-reuse"
    elif lane == "fresh_decode":
        reuse.update(
            {
                "scope": "none",
                "candidate_cache_state": "disabled",
                "exact_response_reuse": False,
                "shared_prefix_tokens": 0,
                "coverage": None,
            }
        )
        binding_lane = "inference.fresh-decode"
    value: dict[str, object] = {
        "schema_version": 1,
        "record_kind": receipt.RECORD_KIND,
        "receipt_id": f"fixture-{lane}",
        "claim_scope": "customer_visible_inference_request_turnaround",
        "lane": lane,
        "scorecard_binding": {
            "lane_id": binding_lane,
            "display_name": f"Fixture {lane}",
            "comparison_group": f"fixture-{lane}",
        },
        "workload": workload,
        "workload_sha256": workload_sha,
        "runtime": {
            "engine_id": "vllm-metal-cx",
            "engine_commit": "1" * 40,
            "metal_runtime_sha256": digest("f"),
            "model_id": "fixture/model",
            "model_revision": "2" * 40,
            "weights_sha256": digest("3"),
            "tokenizer_sha256": digest("4"),
            "precision_id": "q4-test",
            "baseline_config_sha256": digest("5"),
            "candidate_config_sha256": digest("6"),
        },
        "comparison": {
            "claim_axis": "inference_request_turnaround",
            "timing_scope": "integrated_wall",
            "same_logical_work": True,
            "baseline_mode": "target_only",
            "candidate_mode": "ngram",
            "target_multiplier": 50,
        },
        "reuse": reuse,
        "fallback": {
            "enabled": True,
            "mode": "target_only_direct_decode",
            "trigger": "parity_or_confidence_failure",
            "validated": True,
        },
        "authorization": {
            "artifact_verified": False,
            "customer_selectable": False,
            "publication_eligible": False,
            "production_ready": False,
            "billing_eligible": False,
        },
        "attestation": {
            "evidence_class": "physical_local_unattested",
            "independent_attestation": False,
        },
        "artifacts": artifacts,
        "samples": samples,
        "parity": {
            "policy": "exact_output_token_ids",
            "status": "passed",
            "baseline_candidate_exact": True,
            "output_token_ids_sha256": digest("c"),
            "output_token_count": 64,
            "parity_artifact_id": "parity",
        },
        "quality": {
            "policy": "exact_output_token_ids",
            "status": "passed",
            "summary_sha256": digest("d"),
            "quality_artifact_id": "quality",
        },
        "statistics": {
            "unit": "ms/request",
            "baseline": {"p50_ms": 100.0, "p95_ms": 100.0},
            "candidate": {"p50_ms": 1.0, "p95_ms": 1.0},
            "p50_multiplier": 100.0,
            "p95_multiplier": 100.0,
        },
        "receipt_sha256": "",
    }
    value["receipt_sha256"] = receipt.receipt_sha256(value)
    return value


def reseal(value: dict[str, object]) -> None:
    value["receipt_sha256"] = receipt.receipt_sha256(value)


def score_lane_from_projection(projection: dict[str, object], *, source: str) -> dict[str, object]:
    """Construct only the scorecard fields that must mirror a receipt projection."""

    return {
        "lane_id": projection["lane_id"],
        "display_name": projection["display_name"],
        "modality": projection["modality"],
        "metric_kind": projection["metric_kind"],
        "claim_axis": projection["claim_axis"],
        "comparison_group": projection["comparison_group"],
        "measurement": projection["measurement"],
        "quality_gate": projection["quality_gate"],
        "authorization": projection["authorization"],
        "evidence": {"source": source, **projection["evidence"]},
        "guardrail": None,
        "coverage": projection["coverage"],
        "lift": {"limiter": "fixture", "next_experiment": "fixture"},
    }


class InferenceReceiptV1Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.value = build_receipt(self.root)
        self.addCleanup(self.temp.cleanup)

    def test_three_lanes_validate_and_project_separately(self) -> None:
        for lane, expected_id in (
            ("exact_request_reuse", "inference.exact-request-reuse"),
            ("shared_prefix_reuse", "inference.shared-prefix-reuse"),
            ("fresh_decode", "inference.fresh-decode"),
        ):
            with self.subTest(lane=lane):
                value = build_receipt(self.root / lane, lane=lane)
                parsed = receipt.validate_receipt(value)
                evaluation = receipt.evaluate_receipt(parsed)
                projection = receipt.scorecard_projection(parsed, receipt_file_sha256=digest("9"))
                self.assertEqual(evaluation["lane"], lane)
                self.assertEqual(evaluation["scorecard_lane_id"], expected_id)
                self.assertTrue(evaluation["p50_target_met"])
                self.assertTrue(evaluation["p95_target_met"])
                self.assertFalse(evaluation["production_promotion_eligible"])
                self.assertEqual(projection["lane_id"], expected_id)
                self.assertEqual(projection["measurement"]["trials"], 8)
                self.assertEqual(projection["coverage"] is None, lane == "fresh_decode")

    def test_receipt_recomputes_workload_hash_and_self_hash(self) -> None:
        changed_workload = copy.deepcopy(self.value)
        changed_workload["workload"]["request_count"] = 2  # type: ignore[index]
        reseal(changed_workload)
        with self.assertRaisesRegex(receipt.InferenceReceiptError, "workload_sha256"):
            receipt.validate_receipt(changed_workload)

        changed_self_hash = copy.deepcopy(self.value)
        changed_self_hash["receipt_id"] = "a-different-valid-id"
        with self.assertRaisesRegex(receipt.InferenceReceiptError, "receipt_sha256"):
            receipt.validate_receipt(changed_self_hash)

    def test_requires_full_abba_schedule_and_minimum_trials(self) -> None:
        short = copy.deepcopy(self.value)
        short["samples"] = short["samples"][:-4]  # type: ignore[index]
        reseal(short)
        with self.assertRaisesRegex(receipt.InferenceReceiptError, "minimum trials"):
            receipt.validate_receipt(short)

        reordered = copy.deepcopy(self.value)
        reordered["samples"][0]["arm"] = "candidate"  # type: ignore[index]
        reseal(reordered)
        with self.assertRaisesRegex(receipt.InferenceReceiptError, "ABBA"):
            receipt.validate_receipt(reordered)

    def test_recomputes_stage_time_percentiles_and_exact_parity(self) -> None:
        wrong_percentile = copy.deepcopy(self.value)
        wrong_percentile["statistics"]["candidate"]["p95_ms"] = 2.0  # type: ignore[index]
        reseal(wrong_percentile)
        with self.assertRaisesRegex(receipt.InferenceReceiptError, "p95_ms"):
            receipt.validate_receipt(wrong_percentile)

        divergent = copy.deepcopy(self.value)
        divergent["samples"][1]["output_token_ids_sha256"] = digest("0")  # type: ignore[index]
        reseal(divergent)
        with self.assertRaisesRegex(receipt.InferenceReceiptError, "exact-token stable"):
            receipt.validate_receipt(divergent)

    def test_reuse_coverage_and_fresh_cache_off_are_fail_closed(self) -> None:
        bad_coverage = copy.deepcopy(self.value)
        bad_coverage["reuse"]["coverage"]["eligible_hit_rate"] = 0.5  # type: ignore[index]
        reseal(bad_coverage)
        with self.assertRaisesRegex(receipt.InferenceReceiptError, "eligible_hit_rate"):
            receipt.validate_receipt(bad_coverage)

        fresh = build_receipt(self.root / "fresh", lane="fresh_decode")
        fresh["reuse"]["coverage"] = copy.deepcopy(self.value["reuse"]["coverage"])  # type: ignore[index]
        reseal(fresh)
        with self.assertRaisesRegex(receipt.InferenceReceiptError, "fresh_decode forbids"):
            receipt.validate_receipt(fresh)

    def test_reuse_coverage_must_meet_its_declared_minimum(self) -> None:
        below_minimum = copy.deepcopy(self.value)
        coverage = below_minimum["reuse"]["coverage"]  # type: ignore[index]
        coverage["eligible_hits"] = 40
        coverage["eligible_hit_rate"] = 0.5
        reseal(below_minimum)
        with self.assertRaisesRegex(receipt.InferenceReceiptError, "below required"):
            receipt.validate_receipt(below_minimum)

    def test_artifact_byte_binding_is_verified_under_one_root(self) -> None:
        path = self.root / "receipt.json"
        path.write_text(json.dumps(self.value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        parsed, raw_sha = receipt.load_receipt_path(
            path, artifact_root=self.root, verify_artifacts=True
        )
        self.assertEqual(raw_sha, receipt.sha256_bytes(path.read_bytes()))
        self.assertEqual(parsed["receipt_id"], self.value["receipt_id"])

        (self.root / "captures" / "0.json").write_bytes(b"substituted")
        with self.assertRaisesRegex(receipt.InferenceReceiptError, "artifact SHA-256 mismatch"):
            receipt.load_receipt_path(path, artifact_root=self.root, verify_artifacts=True)

    def test_cli_can_require_the_declared_tail_target(self) -> None:
        path = self.root / "receipt.json"
        output = self.root / "evaluation.json"
        path.write_text(json.dumps(self.value), encoding="utf-8")
        self.assertEqual(
            receipt.main(
                [
                    "--receipt",
                    str(path),
                    "--artifact-root",
                    str(self.root),
                    "--verify-artifacts",
                    "--require-tail-target",
                    "--output",
                    str(output),
                ]
            ),
            0,
        )
        result = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(result["p95_target_met"])
        self.assertTrue(result["artifacts_verified"])

    def test_scorecard_binding_requires_the_exact_receipt_projection(self) -> None:
        receipt_path = self.root / "receipt.json"
        receipt_path.write_text(json.dumps(self.value, sort_keys=True), encoding="utf-8")
        raw_sha = receipt.sha256_bytes(receipt_path.read_bytes())
        projection = receipt.scorecard_projection(self.value, receipt_file_sha256=raw_sha)
        lane = score_lane_from_projection(projection, source="receipt.json")
        receipt.verify_artifact_bindings(self.value, self.root)
        binding = score.bind_inference_receipt_to_lane(
            lane,
            self.value,
            receipt_file_sha256=raw_sha,
            scorecard_target_multiplier=50,
            artifacts_verified=True,
            receipt_source="receipt.json",
        )
        self.assertEqual(binding["lane_id"], "inference.exact-request-reuse")
        self.assertTrue(binding["artifact_bindings_verified"])

        changed = copy.deepcopy(lane)
        changed["measurement"]["candidate_value"] = 2.0  # type: ignore[index]
        with self.assertRaisesRegex(score.ScorecardError, "measurement.candidate_value"):
            score.bind_inference_receipt_to_lane(
                changed,
                self.value,
                receipt_file_sha256=raw_sha,
                scorecard_target_multiplier=50,
                artifacts_verified=True,
                receipt_source="receipt.json",
            )

        with self.assertRaisesRegex(score.ScorecardError, "externally verified"):
            score.bind_inference_receipt_to_lane(
                lane,
                self.value,
                receipt_file_sha256=raw_sha,
                scorecard_target_multiplier=50,
                artifacts_verified=False,
                receipt_source="receipt.json",
            )

    def test_scorecard_cli_verifies_artifacts_before_target_check(self) -> None:
        receipt_path = self.root / "receipt.json"
        receipt_path.write_text(json.dumps(self.value, sort_keys=True), encoding="utf-8")
        raw_sha = receipt.sha256_bytes(receipt_path.read_bytes())
        projection = receipt.scorecard_projection(self.value, receipt_file_sha256=raw_sha)
        source = self.root / "scorecard.json"
        source.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "standard_id": "cx-inference-50x-fixture",
                    "as_of": "2026-07-15",
                    "target_multiplier": 50,
                    "lanes": [score_lane_from_projection(projection, source="receipt.json")],
                }
            ),
            encoding="utf-8",
        )
        output = self.root / "score-result.json"
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            unbound_code = score.main(
                [
                    "--scorecard",
                    str(source),
                    "--check-target",
                    "inference.exact-request-reuse",
                    "--output",
                    str(self.root / "unbound-score-result.json"),
                ]
            )
            code = score.main(
                [
                    "--scorecard",
                    str(source),
                    "--bind-inference-receipt",
                    "inference.exact-request-reuse=receipt.json",
                    "--inference-artifact-root",
                    str(self.root),
                    "--check-target",
                    "inference.exact-request-reuse",
                    "--output",
                    str(output),
                ]
            )
        finally:
            os.chdir(previous)
        self.assertEqual(unbound_code, 3)
        self.assertEqual(code, 0)
        result = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(result["inference_receipt_bindings"][0]["trials_per_arm"], 8)


if __name__ == "__main__":
    unittest.main()
