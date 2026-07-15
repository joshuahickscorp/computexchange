#!/usr/bin/env python3
"""Dependency-free tests for the fail-closed vLLM-Metal parity auditor."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import audit_vllm_metal_spec_parity as audit  # noqa: E402


def capture(*, texts: list[str], mode: str = "baseline") -> dict[str, object]:
    return {
        "host": "m3-ultra",
        "runtime": "vllm-metal-pinned",
        "core": "vllm-pinned",
        "backend": "openai",
        "endpoint_type": "openai",
        "model_id": "pinned-model",
        "tokenizer_id": "pinned-tokenizer",
        "workload": "parity-corpus-v1",
        "num_prompts": len(texts),
        "completed": len(texts),
        "failed": 0,
        "errors": [""] * len(texts),
        "total_input_tokens": 10 * len(texts),
        "total_output_tokens": 4 * len(texts),
        "input_lens": [10] * len(texts),
        "output_lens": [4] * len(texts),
        "generated_texts": texts,
        "mode": mode,
    }


def write_capture(path: Path, value: dict[str, object]) -> audit.BenchmarkCapture:
    path.write_text(json.dumps(value), encoding="utf-8")
    return audit.BenchmarkCapture.load(path)


class CaptureValidationTests(unittest.TestCase):
    def test_rejects_summary_that_disagrees_with_lengths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            raw = capture(texts=["one"])
            raw["total_output_tokens"] = 5
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(audit.ParityAuditError, "total_output_tokens"):
                audit.BenchmarkCapture.load(path)

    def test_rejects_failed_or_missing_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            raw = capture(texts=["one"])
            raw["failed"] = 1
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(audit.ParityAuditError, "completed=num_prompts"):
                audit.BenchmarkCapture.load(path)


class AuditTests(unittest.TestCase):
    def _captures(self, baseline_texts: list[str], candidate_texts: list[str]):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        baseline = write_capture(root / "baseline.json", capture(texts=baseline_texts))
        candidate = write_capture(
            root / "candidate.json", capture(texts=candidate_texts, mode="ngram-k3")
        )
        baseline_repeat = write_capture(
            root / "baseline-repeat.json", capture(texts=baseline_texts, mode="baseline-repeat")
        )
        candidate_repeat = write_capture(
            root / "candidate-repeat.json", capture(texts=candidate_texts, mode="ngram-k3-repeat")
        )
        self.addCleanup(self.temp.cleanup)
        return baseline, candidate, baseline_repeat, candidate_repeat

    def test_exact_pair_with_stable_repeats_is_observed_not_promoted(self) -> None:
        baseline, candidate, baseline_repeat, candidate_repeat = self._captures(
            ["same", "identical"], ["same", "identical"]
        )
        receipt = audit.audit_captures(
            baseline,
            candidate,
            baseline_repeat=baseline_repeat,
            candidate_repeat=candidate_repeat,
        )
        self.assertEqual(receipt["status"], "parity_observed")
        self.assertTrue(receipt["guard"]["candidate_lossless_for_this_matrix"])
        self.assertFalse(receipt["guard"]["speed_claim_valid"])
        self.assertFalse(receipt["guard"]["production_enablement_valid"])

    def test_one_byte_difference_fails_closed_and_reports_only_digests(self) -> None:
        baseline, candidate, baseline_repeat, candidate_repeat = self._captures(
            ["prefix\n\nnext"], ["prefix\nnext"]
        )
        receipt = audit.audit_captures(
            baseline,
            candidate,
            baseline_repeat=baseline_repeat,
            candidate_repeat=candidate_repeat,
        )
        self.assertEqual(receipt["status"], "parity_failed")
        self.assertIn("baseline_candidate_output_mismatch", receipt["guard"]["reason_codes"])
        mismatch = receipt["correctness"]["first_mismatch"]
        self.assertEqual(mismatch["prompt_index_zero_based"], 0)
        self.assertEqual(mismatch["first_byte_difference"]["utf8_byte_offset"], 7)
        self.assertNotIn("baseline_text", mismatch)
        self.assertNotIn("candidate_text", mismatch)

    def test_exact_pair_without_repeats_remains_inconclusive(self) -> None:
        baseline, candidate, _, _ = self._captures(["same"], ["same"])
        receipt = audit.audit_captures(baseline, candidate)
        self.assertEqual(receipt["status"], "inconclusive_missing_repeats")
        self.assertFalse(receipt["guard"]["candidate_lossless_for_this_matrix"])
        self.assertIn("missing_stability_repeat", receipt["guard"]["reason_codes"])

    def test_identity_change_fails_before_outputs_can_be_called_comparable(self) -> None:
        baseline, candidate, baseline_repeat, candidate_repeat = self._captures(
            ["same"], ["same"]
        )
        changed = copy.deepcopy(candidate.identity)
        changed["model_id"] = "other-model"
        candidate = audit.BenchmarkCapture(
            source=candidate.source,
            source_sha256=candidate.source_sha256,
            identity=changed,
            generated_texts=candidate.generated_texts,
            output_array_sha256=candidate.output_array_sha256,
        )
        receipt = audit.audit_captures(
            baseline,
            candidate,
            baseline_repeat=baseline_repeat,
            candidate_repeat=candidate_repeat,
        )
        self.assertEqual(receipt["status"], "parity_failed")
        self.assertEqual(receipt["comparability"]["identity_mismatch_fields"], ["model_id"])

    def test_repeat_instability_is_a_failure_even_if_one_pair_matches(self) -> None:
        baseline, candidate, baseline_repeat, candidate_repeat = self._captures(
            ["same"], ["same"]
        )
        unstable = audit.BenchmarkCapture(
            source=candidate_repeat.source,
            source_sha256=candidate_repeat.source_sha256,
            identity=candidate_repeat.identity,
            generated_texts=("different",),
            output_array_sha256=audit.sha256_json(["different"]),
        )
        receipt = audit.audit_captures(
            baseline,
            candidate,
            baseline_repeat=baseline_repeat,
            candidate_repeat=unstable,
        )
        self.assertEqual(receipt["status"], "parity_failed")
        self.assertIn("candidate_repeat_not_stable", receipt["guard"]["reason_codes"])

    def test_token_difference_reports_ids_but_not_decoded_strings(self) -> None:
        self.assertEqual(
            audit.first_token_difference([1, 1875, 3], [1, 702, 3]),
            {
                "token_index": 1,
                "baseline_token_id": 1875,
                "candidate_token_id": 702,
            },
        )
        self.assertEqual(
            audit.first_token_difference([1], [1, 2]),
            {
                "token_index": 1,
                "baseline_token_id": -1,
                "candidate_token_id": 2,
            },
        )


if __name__ == "__main__":
    raise SystemExit(unittest.main())
