#!/usr/bin/env python3
"""Adversarial tests for the independent render-quality-v3 verifier."""

from __future__ import annotations

import copy
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
from PIL import Image


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cx_render_quality_v3 as quality  # noqa: E402
import verify_cx_render_quality_v3 as verifier  # noqa: E402


WIDTH = 96
HEIGHT = 64


def rich_rgb() -> np.ndarray:
    y, x = np.mgrid[0:HEIGHT, 0:WIDTH]
    checker = ((x // 2 + y // 2) % 2).astype(np.float64)
    red = np.clip(0.05 + 0.65 * x / (WIDTH - 1) + 0.25 * checker, 0.0, 1.0)
    green = np.clip(
        0.05 + 0.65 * y / (HEIGHT - 1) + 0.25 * (1.0 - checker),
        0.0,
        1.0,
    )
    blue = np.clip(
        0.10 + 0.50 * (1.0 - x / (WIDTH - 1)) + 0.30 * checker,
        0.0,
        1.0,
    )
    return np.rint(np.stack((red, green, blue), axis=2) * 255.0).astype(
        np.uint8
    )


def png_bytes(array: np.ndarray) -> bytes:
    output = BytesIO()
    Image.fromarray(array, "RGB").save(output, format="PNG", compress_level=0)
    return output.getvalue()


class VerifierFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.candidate = self.root / "candidate.png"
        self.reference = self.root / "reference.png"
        self.proof = self.root / "quality-v3.json"
        self.reference_bytes = png_bytes(rich_rgb())
        self.candidate.write_bytes(self.reference_bytes)
        self.reference.write_bytes(self.reference_bytes)
        self.result = quality.evaluate_png_bytes(
            self.reference_bytes,
            self.reference_bytes,
            target_size=(WIDTH, HEIGHT),
        )
        self.assertTrue(self.result["pass"])
        self.write_proof(self.result)

    def write_proof(self, value: object) -> None:
        self.proof.write_text(
            json.dumps(
                value,
                sort_keys=True,
                indent=2,
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n",
            encoding="ascii",
        )

    def verify(self) -> dict[str, object]:
        return verifier.verify_paths(
            self.proof, self.candidate, self.reference
        )

    def test_valid_proof_recomputes_and_binds_every_identity(self) -> None:
        first = self.verify()
        second = self.verify()
        self.assertEqual(first, second)
        self.assertTrue(first["proof_verified"])
        self.assertTrue(first["quality_pass"])
        self.assertTrue(first["pass"])
        self.assertEqual(first["errors"], [])
        self.assertEqual(
            first["proof_result_sha256"], first["recomputed_result_sha256"]
        )
        self.assertEqual(
            first["artifacts"]["candidate"],
            {
                "bytes": self.result["inputs"]["candidate"]["bytes"],
                "sha256": self.result["inputs"]["candidate"]["sha256"],
            },
        )
        self.assertEqual(
            first["evaluator"]["metric_module_sha256"],
            self.result["runtime"]["metric_module_sha256"],
        )
        json.dumps(first, sort_keys=True, allow_nan=False)

    def test_valid_failed_quality_decision_is_verified_but_not_accepted(self) -> None:
        black = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
        candidate_bytes = png_bytes(black)
        self.candidate.write_bytes(candidate_bytes)
        failed = quality.evaluate_png_bytes(
            candidate_bytes,
            self.reference_bytes,
            target_size=(WIDTH, HEIGHT),
        )
        self.assertFalse(failed["pass"])
        self.write_proof(failed)
        result = self.verify()
        self.assertTrue(result["proof_verified"])
        self.assertFalse(result["quality_pass"])
        self.assertFalse(result["pass"])
        self.assertEqual(result["errors"], [])

    def test_valid_malformed_artifact_rejection_can_still_be_verified(self) -> None:
        malformed = b"not a png"
        self.candidate.write_bytes(malformed)
        rejected = quality.evaluate_png_bytes(
            malformed,
            self.reference_bytes,
            target_size=(WIDTH, HEIGHT),
        )
        self.assertEqual(
            rejected["errors"], ["candidate:invalid_png_signature"]
        )
        self.write_proof(rejected)
        result = self.verify()
        self.assertTrue(result["proof_verified"])
        self.assertFalse(result["quality_pass"])
        self.assertFalse(result["pass"])
        self.assertEqual(result["errors"], [])

    def test_artifact_substitution_and_symlink_are_rejected(self) -> None:
        replacement = png_bytes(np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8))
        self.candidate.write_bytes(replacement)
        result = self.verify()
        self.assertEqual(result["errors"], ["candidate_artifact_binding_mismatch"])
        self.assertFalse(result["proof_verified"])

        self.candidate.unlink()
        target = self.root / "candidate-real.png"
        target.write_bytes(self.reference_bytes)
        try:
            self.candidate.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        result = self.verify()
        self.assertEqual(result["errors"], ["candidate_not_regular"])

    def test_metric_and_decision_tampering_reject_at_exact_wire_value(self) -> None:
        metric = copy.deepcopy(self.result)
        metric["mattes"]["black"]["metrics"]["gaussian_luma_ssim"][
            "value"
        ] = 0.999999999
        self.write_proof(metric)
        result = self.verify()
        self.assertEqual(result["errors"], ["proof_recomputation_mismatch"])
        self.assertNotEqual(
            result["proof_result_sha256"], result["recomputed_result_sha256"]
        )

        decision = copy.deepcopy(self.result)
        decision["pass"] = False
        self.write_proof(decision)
        self.assertEqual(
            self.verify()["errors"], ["proof_recomputation_mismatch"]
        )

    def test_missing_and_extra_fields_reject_at_every_level(self) -> None:
        mutations: list[tuple[str, dict[str, object], str]] = []
        missing_top = copy.deepcopy(self.result)
        missing_top.pop("mattes")
        mutations.append(
            ("missing-top", missing_top, "proof_result_shape_mismatch")
        )
        extra_top = copy.deepcopy(self.result)
        extra_top["surprise"] = True
        mutations.append(("extra-top", extra_top, "proof_result_shape_mismatch"))
        missing_nested = copy.deepcopy(self.result)
        missing_nested["mattes"]["black"]["metrics"].pop("sobel_gmsd")
        mutations.append(
            ("missing-nested", missing_nested, "proof_recomputation_mismatch")
        )
        extra_nested = copy.deepcopy(self.result)
        extra_nested["mattes"]["white"]["metrics"]["surprise"] = {
            "value": 1.0,
            "pass": True,
        }
        mutations.append(
            ("extra-nested", extra_nested, "proof_recomputation_mismatch")
        )
        for label, value, expected in mutations:
            with self.subTest(label=label):
                self.write_proof(value)
                self.assertEqual(self.verify()["errors"], [expected])

    def test_contract_runtime_and_claimed_digest_tampering_reject(self) -> None:
        contract = copy.deepcopy(self.result)
        contract["contract"]["id"] = "lookalike-contract"
        self.write_proof(contract)
        self.assertEqual(
            self.verify()["errors"], ["proof_contract_identity_mismatch"]
        )

        runtime = copy.deepcopy(self.result)
        runtime["runtime"]["metric_module_sha256"] = "0" * 64
        self.write_proof(runtime)
        self.assertEqual(
            self.verify()["errors"], ["proof_evaluator_identity_mismatch"]
        )

        binding = copy.deepcopy(self.result)
        binding["inputs"]["candidate"]["sha256"] = "f" * 64
        self.write_proof(binding)
        self.assertEqual(
            self.verify()["errors"], ["candidate_artifact_binding_mismatch"]
        )

    def test_verifier_has_independent_source_and_contract_pins(self) -> None:
        with mock.patch.object(
            verifier, "EXPECTED_EVALUATOR_SHA256", "0" * 64
        ):
            self.assertEqual(
                self.verify()["errors"], ["evaluator_source_pin_mismatch"]
            )
        with mock.patch.object(
            verifier, "EXPECTED_CONTRACT_SHA256", "f" * 64
        ):
            self.assertEqual(
                self.verify()["errors"], ["evaluator_contract_pin_mismatch"]
            )

    def test_duplicate_nonfinite_malformed_and_nonobject_json_reject(self) -> None:
        cases = {
            "duplicate": (b'{"x":1,"x":2}\n', "proof_duplicate_key"),
            "nan": (b'{"x":NaN}\n', "proof_nonfinite_number"),
            "overflow": (b'{"x":1e999}\n', "proof_nonfinite_number"),
            "malformed": (b'{"x":}\n', "proof_json_invalid"),
            "nonobject": (b'[]\n', "proof_not_object"),
        }
        for label, (payload, expected) in cases.items():
            with self.subTest(label=label):
                self.proof.write_bytes(payload)
                result = self.verify()
                self.assertEqual(result["errors"], [expected])
                json.dumps(result, sort_keys=True, allow_nan=False)

    def test_cli_is_deterministic_and_gate_exit_tracks_quality(self) -> None:
        command = [
            sys.executable,
            str(HERE / "verify_cx_render_quality_v3.py"),
            str(self.proof),
            str(self.candidate),
            str(self.reference),
        ]
        first = subprocess.run(command, check=False, capture_output=True, text=True)
        second = subprocess.run(command, check=False, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(first.stderr, "")
        payload = json.loads(first.stdout)
        self.assertTrue(payload["pass"])

        black = png_bytes(np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8))
        self.candidate.write_bytes(black)
        failed = quality.evaluate_png_bytes(
            black, self.reference_bytes, target_size=(WIDTH, HEIGHT)
        )
        self.write_proof(failed)
        rejected = subprocess.run(
            command, check=False, capture_output=True, text=True
        )
        self.assertEqual(rejected.returncode, 1, rejected.stderr)
        rejected_payload = json.loads(rejected.stdout)
        self.assertTrue(rejected_payload["proof_verified"])
        self.assertFalse(rejected_payload["quality_pass"])
        self.assertEqual(rejected_payload["errors"], [])


if __name__ == "__main__":
    unittest.main()
