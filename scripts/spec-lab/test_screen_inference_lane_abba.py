#!/usr/bin/env python3
"""Tests for the isolated inference-lane ABBA measurement contract.

The fake runner below writes synthetic receipts only; it never launches an
inference runtime or makes a performance claim.  The assertions exercise the
contract's validation and receipt wiring rather than any model behavior.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import screen_inference_lane_abba as subject  # noqa: E402
import cx_inference_receipt_v1 as strict_receipt  # noqa: E402
import score_1000x_lanes as score  # noqa: E402


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(subject._canonical_json(value) + b"\n")


class TickClock:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> int:
        self.value += 1_000_000
        return self.value


class FakeInferenceRunner:
    """Receipt-producing test double; it deliberately does not execute argv."""

    def __init__(
        self,
        owner: "InferenceLaneAbbaTests",
        *,
        mismatch: bool = False,
        invalid_reuse: bool = False,
        unvalidated_fallback: bool = False,
    ) -> None:
        self.owner = owner
        self.mismatch = mismatch
        self.invalid_reuse = invalid_reuse
        self.unvalidated_fallback = unvalidated_fallback

    def __call__(self, argv: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        arm = "candidate" if "--arm=candidate" in argv else "baseline"
        result_path = Path(argv[2])
        stage_paths = {stage: Path(argv[index + 3]) for index, stage in enumerate(subject.STAGES)}
        manifest = self.owner.manifest_values[arm]
        logical = manifest["logical_work"]
        runtime_sha = subject.sha256_json(manifest["runtime"])
        cache_sha = subject.sha256_json(manifest["cache_policy"])
        outputs = []
        token_lists: list[list[int]] = [[11, 12], [13]]
        if arm == "candidate" and self.mismatch:
            token_lists[0] = [99]
        for index, tokens in enumerate(token_lists):
            outputs.append(
                {
                    "request_index": index,
                    "request_sha256": logical["request_digests"][index],
                    "completion_token_ids": tokens,
                    "completion_token_ids_sha256": subject.sha256_json(tokens),
                    "completion_token_count": len(tokens),
                }
            )
        delivery_digest = subject.sha256_json([subject.sha256_json(tokens) for tokens in token_lists])
        stage_hashes: dict[str, str] = {}
        for stage in subject.STAGES:
            artifact = delivery_digest if stage == "delivery" else _sha_text(f"{arm}:{stage}")
            value = {
                "schema_version": subject.SCHEMA_VERSION,
                "kind": subject.STAGE_RECEIPT_KIND,
                "stage": stage,
                "arm": arm,
                "lane": manifest["lane"],
                "logical_work_sha256": subject.sha256_json(logical),
                "runtime_identity_sha256": runtime_sha,
                "cache_policy_sha256": cache_sha,
                "completed": True,
                "included_in_end_to_end_wall": True,
                "elapsed_ns": 1,
                "artifact_sha256": artifact,
            }
            _write(stage_paths[stage], value)
            stage_hashes[stage] = hashlib.sha256(stage_paths[stage].read_bytes()).hexdigest()
        eligible = set(logical["reuse_contract"]["eligible_request_indexes"])
        reuse_outcomes = []
        for index in range(len(logical["request_digests"])):
            hit = arm == "candidate" and manifest["lane"] != "fresh_decode" and index in eligible
            if arm == "candidate" and self.invalid_reuse and index == 1:
                hit = True
            reuse_outcomes.append({"request_index": index, "eligible": index in eligible, "hit": hit})
        result = {
            "schema_version": subject.SCHEMA_VERSION,
            "kind": subject.ARM_RESULT_KIND,
            "arm": arm,
            "lane": manifest["lane"],
            "logical_work_sha256": subject.sha256_json(logical),
            "runtime_identity_sha256": runtime_sha,
            "cache_policy_sha256": cache_sha,
            "outputs": outputs,
            "reuse_outcomes": reuse_outcomes,
            "stage_receipts": stage_hashes,
            "fallback": {
                "direct_target_decode_available": True,
                "direct_target_decode_validated": not self.unvalidated_fallback,
                "used": False,
                "reason_code": "none",
            },
            "delivery_output_sha256": delivery_digest,
        }
        _write(result_path, result)
        return subprocess.CompletedProcess(args=argv, returncode=0)


class InferenceLaneAbbaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cx-inference-lane-contract-")
        self.root = Path(self.temporary.name)
        self.executable = self.root / "receipt-runner"
        self.executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.executable.chmod(self.executable.stat().st_mode | stat.S_IXUSR)
        self.request_digests = [_sha_text("request-0"), _sha_text("request-1")]
        self.manifest_values: dict[str, dict[str, object]] = {}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _logical_work(self, lane: str, *, concurrency: int = 1) -> dict[str, object]:
        if lane == "exact_request_reuse":
            reuse = {
                "eligible_request_indexes": [0],
                "exact_request_key_schema_sha256": "a" * 64,
                "shared_prefix_token_ids_sha256": None,
                "shared_prefix_token_count": 0,
                "required_eligible_hit_rate": 1.0,
            }
        elif lane == "shared_prefix_reuse":
            reuse = {
                "eligible_request_indexes": [0],
                "exact_request_key_schema_sha256": None,
                "shared_prefix_token_ids_sha256": "b" * 64,
                "shared_prefix_token_count": 128,
                "required_eligible_hit_rate": 1.0,
            }
        else:
            reuse = {
                "eligible_request_indexes": [],
                "exact_request_key_schema_sha256": None,
                "shared_prefix_token_ids_sha256": None,
                "shared_prefix_token_count": 0,
                "required_eligible_hit_rate": None,
            }
        sampling = {"temperature": 0, "top_p": 1, "seed": 0, "n": 1}
        return {
            "model": {
                "model_id": "mlx-community/Llama-3.2-1B-Instruct-4bit",
                "model_revision": "c" * 40,
                "tokenizer_id": "mlx-community/Llama-3.2-1B-Instruct-4bit",
                "tokenizer_revision": "c" * 40,
            },
            "corpus_sha256": "d" * 64,
            "request_digests": list(self.request_digests),
            "request_order_sha256": subject.sha256_json(self.request_digests),
            "input_token_ids_sha256": "e" * 64,
            "sampling_contract_sha256": subject.sha256_json(sampling),
            "sampling": sampling,
            "max_output_tokens": 16,
            "concurrency": concurrency,
            "reuse_contract": reuse,
        }

    def _runtime(self, arm: str) -> dict[str, str]:
        return {
            "backend": "metal",
            "host_hardware_sha256": "1" * 64,
            "core_sha256": "2" * 64,
            "runtime_sha256": "3" * 64,
            "resolved_engine_config_sha256": ("4" if arm == "baseline" else "5") * 64,
            "engine_id": "vllm-metal-cx",
            "engine_commit": "a" * 40,
            "metal_runtime_sha256": "6" * 64,
            "weights_sha256": "7" * 64,
            "tokenizer_sha256": "8" * 64,
            "precision_id": "q4-test",
        }

    def _cache_policy(self, lane: str, arm: str) -> dict[str, object]:
        if arm == "baseline":
            return {
                "mode": "target_only_no_reuse",
                "cache_history_sha256": "6" * 64,
                "response_cache_enabled": False,
                "prefix_cache_enabled": False,
                "fallback_policy_sha256": "7" * 64,
                "proposer_config_sha256": None,
            }
        if lane == "exact_request_reuse":
            return {
                "mode": "exact_request_reuse",
                "cache_history_sha256": "8" * 64,
                "response_cache_enabled": True,
                "prefix_cache_enabled": False,
                "fallback_policy_sha256": "7" * 64,
                "proposer_config_sha256": "9" * 64,
            }
        if lane == "shared_prefix_reuse":
            return {
                "mode": "shared_prefix_reuse",
                "cache_history_sha256": "8" * 64,
                "response_cache_enabled": False,
                "prefix_cache_enabled": True,
                "fallback_policy_sha256": "7" * 64,
                "proposer_config_sha256": "9" * 64,
            }
        return {
            "mode": "fresh_decode_candidate",
            "cache_history_sha256": "8" * 64,
            "response_cache_enabled": False,
            "prefix_cache_enabled": False,
            "fallback_policy_sha256": "7" * 64,
            "proposer_config_sha256": "9" * 64,
        }

    def _manifest(self, arm: str, lane: str, *, logical: dict[str, object] | None = None) -> Path:
        logical_work = self._logical_work(lane) if logical is None else logical
        result_template = "{trial_dir}/arm-result.json"
        stage_templates = {stage: f"{{trial_dir}}/{stage}.json" for stage in subject.STAGES}
        value: dict[str, object] = {
            "schema_version": subject.SCHEMA_VERSION,
            "kind": subject.ARM_MANIFEST_KIND,
            "arm": arm,
            "lane": lane,
            "logical_work": logical_work,
            "runtime": self._runtime(arm),
            "cache_policy": self._cache_policy(lane, arm),
            "authorization": {
                "artifact_verified": False,
                "customer_selectable": False,
                "publication_eligible": False,
                "production_ready": False,
                "billing_eligible": False,
            },
            "command": {
                "argv": [str(self.executable.resolve()), f"--arm={arm}", result_template, *(stage_templates[stage] for stage in subject.STAGES)],
                "timeout_secs": 10,
                "pinned_files": [
                    {
                        "role": "command_executable",
                        "path": str(self.executable.resolve()),
                        "sha256": hashlib.sha256(self.executable.read_bytes()).hexdigest(),
                    }
                ],
            },
            "trial_outputs": {"arm_result": result_template, "stages": stage_templates},
        }
        self.manifest_values[arm] = value
        path = self.root / f"{arm}-{lane}.json"
        _write(path, value)
        return path

    def _config(
        self,
        baseline: Path,
        candidate: Path,
        suffix: str,
        *,
        qualifying_receipt_out: Path | None = None,
        target_multiplier: float = 50.0,
    ) -> subject.RunConfig:
        return subject.RunConfig(
            baseline_manifest=baseline,
            candidate_manifest=candidate,
            work_root=self.root / f"work-{suffix}",
            receipt_out=self.root / f"receipt-{suffix}.json",
            trials=8,
            qualifying_receipt_out=qualifying_receipt_out,
            target_multiplier=target_multiplier,
        )

    def test_status_has_three_separate_lanes_and_no_claim(self) -> None:
        status = subject.unmeasured_contract()
        self.assertEqual(status["lanes"], list(subject.LANES))
        self.assertIsNone(status["claim"]["direct_end_to_end_p50_multiplier"])
        self.assertIsNone(status["claim"]["aggregate_multiplier"])
        self.assertFalse(status["claim"]["eligible_for_public_50x_claim"])
        self.assertEqual(status["required_evidence"]["minimum_trials_per_arm"], 8)
        self.assertEqual(
            status["required_evidence"]["strict_receipt_adapter"],
            "--qualifying-receipt-out",
        )
        self.assertEqual(subject.repeatable_trial_orders(8), subject.ABBA_ORDERS * 2)
        with self.assertRaisesRegex(subject.InferenceLaneContractError, "abba"):
            subject.repeatable_trial_orders(4)

    def test_exact_reuse_measures_one_complete_boundary_and_observed_coverage(self) -> None:
        baseline = self._manifest("baseline", "exact_request_reuse")
        candidate = self._manifest("candidate", "exact_request_reuse")
        qualifying_receipt = self.root / "qualifying-exact.json"
        config = self._config(
            baseline,
            candidate,
            "exact",
            qualifying_receipt_out=qualifying_receipt,
        )
        receipt = subject.benchmark(config, clock_ns=TickClock(), runner=FakeInferenceRunner(self))

        self.assertEqual(receipt["measurement_status"], "real_paired_trials_recorded_local_unattested")
        self.assertEqual(receipt["lane"], "exact_request_reuse")
        self.assertEqual(receipt["claim"]["direct_end_to_end_p50_multiplier"], 1.0)
        self.assertIsNone(receipt["claim"]["aggregate_multiplier"])
        self.assertFalse(receipt["claim"]["eligible_for_public_50x_claim"])
        self.assertFalse(receipt["timing_contract"]["substage_elapsed_ns_used_for_ratio"])
        self.assertEqual(receipt["reuse_coverage"]["eligible_request_opportunities"], 8)
        self.assertEqual(receipt["reuse_coverage"]["observed_hits"], 8)
        self.assertEqual(receipt["reuse_coverage"]["observed_eligible_hit_rate"], 1.0)
        self.assertTrue(receipt["promotion_gate"]["exact_token_parity_each_pair"])
        self.assertTrue(config.receipt_out.is_file())
        self.assertEqual(json.loads(config.receipt_out.read_text()), receipt)
        self.assertNotIn("stage_multiplier", json.dumps(receipt, sort_keys=True))
        qualifying = json.loads(qualifying_receipt.read_text(encoding="utf-8"))
        strict_receipt.validate_receipt(qualifying)
        strict_receipt.verify_artifact_bindings(qualifying, config.work_root)
        self.assertEqual(qualifying["workload"]["customer_visible_stages"], list(subject.STAGES))
        self.assertEqual(len(qualifying["samples"]), 16)
        self.assertEqual(
            [sample["arm"] for sample in qualifying["samples"]],
            list(strict_receipt.ABBA * 4),
        )
        self.assertEqual(qualifying["statistics"]["p50_multiplier"], 1.0)
        projection = strict_receipt.scorecard_projection(
            qualifying,
            receipt_file_sha256=strict_receipt.sha256_bytes(qualifying_receipt.read_bytes()),
        )
        self.assertEqual(projection["measurement"]["trials"], 8)
        self.assertEqual(projection["lane_id"], "inference.exact-request-reuse")
        scorecard_lane = {
            "lane_id": projection["lane_id"],
            "display_name": projection["display_name"],
            "modality": projection["modality"],
            "metric_kind": projection["metric_kind"],
            "claim_axis": projection["claim_axis"],
            "comparison_group": projection["comparison_group"],
            "measurement": projection["measurement"],
            "quality_gate": projection["quality_gate"],
            "authorization": projection["authorization"],
            "evidence": {"source": "qualifying-exact.json", **projection["evidence"]},
            "guardrail": None,
            "coverage": projection["coverage"],
            "lift": {"limiter": "synthetic fixture", "next_experiment": "real ABBA run"},
        }
        binding = score.bind_inference_receipt_to_lane(
            scorecard_lane,
            qualifying,
            receipt_file_sha256=strict_receipt.sha256_bytes(qualifying_receipt.read_bytes()),
            scorecard_target_multiplier=50,
            artifacts_verified=True,
            receipt_source="qualifying-exact.json",
        )
        self.assertEqual(binding["trials_per_arm"], 8)

    def test_prefix_and_fresh_lanes_do_not_share_reuse_semantics(self) -> None:
        prefix_baseline = self._manifest("baseline", "shared_prefix_reuse")
        prefix_candidate = self._manifest("candidate", "shared_prefix_reuse")
        prefix = subject.benchmark(
            self._config(prefix_baseline, prefix_candidate, "prefix"),
            clock_ns=TickClock(),
            runner=FakeInferenceRunner(self),
        )
        self.assertEqual(prefix["reuse_coverage"]["observed_eligible_hit_rate"], 1.0)

        fresh_baseline = self._manifest("baseline", "fresh_decode")
        fresh_candidate = self._manifest("candidate", "fresh_decode")
        fresh = subject.benchmark(
            self._config(fresh_baseline, fresh_candidate, "fresh"),
            clock_ns=TickClock(),
            runner=FakeInferenceRunner(self),
        )
        self.assertIsNone(fresh["reuse_coverage"]["observed_eligible_hit_rate"])
        self.assertTrue(fresh["reuse_coverage"]["not_applicable_for_fresh_decode"])

    def test_declared_25x_target_is_bound_into_raw_and_strict_receipts(self) -> None:
        baseline = self._manifest("baseline", "shared_prefix_reuse")
        candidate = self._manifest("candidate", "shared_prefix_reuse")
        qualifying_receipt = self.root / "qualifying-prefix-25x.json"
        config = self._config(
            baseline,
            candidate,
            "prefix-25x",
            qualifying_receipt_out=qualifying_receipt,
            target_multiplier=25.0,
        )
        raw = subject.benchmark(config, clock_ns=TickClock(), runner=FakeInferenceRunner(self))
        self.assertEqual(raw["claim"]["declared_target_multiplier"], 25.0)
        self.assertEqual(raw["promotion_gate"]["declared_target_multiplier"], 25.0)
        self.assertFalse(raw["promotion_gate"]["p50_reaches_declared_target"])
        self.assertFalse(raw["promotion_gate"]["p95_reaches_declared_target"])
        strict = json.loads(qualifying_receipt.read_text(encoding="utf-8"))
        self.assertEqual(strict["comparison"]["target_multiplier"], 25.0)
        evaluation = strict_receipt.evaluate_receipt(strict)
        self.assertEqual(evaluation["target_multiplier"], 25.0)
        self.assertFalse(evaluation["p50_target_met"])

    def test_declared_target_must_be_finite_and_at_least_one(self) -> None:
        baseline = self._manifest("baseline", "fresh_decode")
        candidate = self._manifest("candidate", "fresh_decode")
        for index, target in enumerate((0.0, float("nan"), True)):
            with self.subTest(target=target):
                config = self._config(baseline, candidate, f"invalid-target-{index}", target_multiplier=target)
                with self.assertRaisesRegex(subject.InferenceLaneContractError, "target_multiplier"):
                    config.validate()

    def test_exact_token_difference_fails_closed_without_receipt(self) -> None:
        baseline = self._manifest("baseline", "exact_request_reuse")
        candidate = self._manifest("candidate", "exact_request_reuse")
        config = self._config(baseline, candidate, "mismatch")
        with self.assertRaisesRegex(subject.InferenceLaneContractError, "exact_token_parity_failed"):
            subject.benchmark(config, clock_ns=TickClock(), runner=FakeInferenceRunner(self, mismatch=True))
        self.assertFalse(config.receipt_out.exists())

    def test_hit_outside_predeclared_eligibility_fails_closed(self) -> None:
        baseline = self._manifest("baseline", "exact_request_reuse")
        candidate = self._manifest("candidate", "exact_request_reuse")
        config = self._config(baseline, candidate, "bad-reuse")
        with self.assertRaisesRegex(subject.InferenceLaneContractError, "reuse_hit_without_predeclared"):
            subject.benchmark(config, clock_ns=TickClock(), runner=FakeInferenceRunner(self, invalid_reuse=True))
        self.assertFalse(config.receipt_out.exists())

    def test_unvalidated_direct_target_fallback_fails_before_a_qualifying_receipt(self) -> None:
        baseline = self._manifest("baseline", "exact_request_reuse")
        candidate = self._manifest("candidate", "exact_request_reuse")
        qualifying_receipt = self.root / "qualifying-unvalidated-fallback.json"
        config = self._config(
            baseline,
            candidate,
            "unvalidated-fallback",
            qualifying_receipt_out=qualifying_receipt,
        )
        with self.assertRaisesRegex(subject.InferenceLaneContractError, "fallback_not_validated"):
            subject.benchmark(
                config,
                clock_ns=TickClock(),
                runner=FakeInferenceRunner(self, unvalidated_fallback=True),
            )
        self.assertFalse(config.receipt_out.exists())
        self.assertFalse(qualifying_receipt.exists())

    def test_fresh_candidate_cache_reuse_is_rejected_at_manifest_preflight(self) -> None:
        baseline = self._manifest("baseline", "fresh_decode")
        candidate = self._manifest("candidate", "fresh_decode")
        raw = json.loads(candidate.read_text())
        raw["cache_policy"]["response_cache_enabled"] = True
        _write(candidate, raw)
        with self.assertRaisesRegex(subject.InferenceLaneContractError, "fresh_decode_candidate_cache_policy_invalid"):
            subject.benchmark(self._config(baseline, candidate, "fresh-bad-cache"), clock_ns=TickClock(), runner=FakeInferenceRunner(self))

    def test_logical_work_change_cannot_be_compared(self) -> None:
        baseline = self._manifest("baseline", "exact_request_reuse")
        changed = self._logical_work("exact_request_reuse", concurrency=2)
        candidate = self._manifest("candidate", "exact_request_reuse", logical=changed)
        with self.assertRaisesRegex(subject.InferenceLaneContractError, "logical_work_mismatch"):
            subject.benchmark(self._config(baseline, candidate, "changed-work"), clock_ns=TickClock(), runner=FakeInferenceRunner(self))


if __name__ == "__main__":
    raise SystemExit(unittest.main())
