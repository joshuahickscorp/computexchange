#!/usr/bin/env python3
"""Mutation tests for the bounded BMW27 integrator-screen verifier."""

from __future__ import annotations

import copy
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import verify_render_bmw_integrator_screen as verifier  # noqa: E402
import verify_render_transfer_matrix as common  # noqa: E402


class BMWIntegratorScreenVerifierTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.proof_path = self.root / "proof.json"
        shutil.copy2(verifier.DEFAULT_PROOF, self.proof_path)

    def load(self) -> dict[str, object]:
        return json.loads(self.proof_path.read_text(encoding="utf-8"))

    @staticmethod
    def write(path: Path, value: dict[str, object]) -> None:
        path.write_text(
            json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )

    def verify(self, *, require_local: bool = False) -> dict[str, object]:
        return verifier.verify(
            self.proof_path, require_local_artifacts=require_local
        )

    def test_current_proof_and_all_local_artifacts_pass_as_negative_result(self) -> None:
        result = verifier.verify(require_local_artifacts=True)
        self.assertTrue(result["valid"])
        self.assertTrue(result["local_artifacts_verified"])
        self.assertEqual(result["local_receipts_verified"], 8)
        self.assertEqual(result["local_artifact_arms_verified"], 8)
        self.assertEqual(result["retained_4096_audits_recomputed"], 8)
        self.assertEqual(result["best_arm"], "cap8_v1")
        self.assertEqual(result["best_cross_session_projection_x"], 40.292637)
        self.assertEqual(result["native_cross_session_projection_x"], 40.07152)
        self.assertFalse(result["cross_session_projection_is_measured"])
        self.assertFalse(result["meets_50x"])
        self.assertFalse(result["production_ready"])

    def test_projection_and_quality_mutations_fail_semantic_validation(self) -> None:
        proof = self.load()
        proof["arms"][3]["projection"]["projected_x"] = 50.0
        with self.assertRaisesRegex(ValueError, r"projection\.projected_x"):
            verifier._validate_semantics(proof)

        proof = self.load()
        proof["arms"][3]["product_gate"]["worst_microtile_agreement"] = 0.69
        proof["arms"][3]["product_gate"]["passed"] = False
        with self.assertRaisesRegex(ValueError, "unexpectedly failed"):
            verifier._validate_semantics(proof)

    def test_exact_keys_duplicate_keys_and_nonfinite_numbers_are_rejected(self) -> None:
        proof = self.load()
        proof["unexpected"] = True
        self.write(self.proof_path, proof)
        with self.assertRaisesRegex(ValueError, "field set mismatch"):
            self.verify()

        self.proof_path.write_text(
            '{"schema_version":1,"schema_version":1}\n', encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "duplicate key"):
            self.verify()

        raw = verifier.DEFAULT_PROOF.read_text(encoding="utf-8")
        self.proof_path.write_text(
            raw.replace('"best_product_s": 1.472072', '"best_product_s": NaN', 1),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "non-finite"):
            self.verify()

    def test_portable_proof_passes_without_cache_unless_local_is_required(self) -> None:
        proof = self.load()
        proof["protocol"]["historical_4096_reference"]["artifact_root"] = str(
            self.root / "absent-historical-root"
        )
        for index, arm in enumerate(proof["arms"]):
            arm["receipt"]["path"] = str(self.root / f"absent-receipt-{index}.json")
            arm["local_artifacts"]["root"] = str(
                self.root / f"absent-artifacts-{index}"
            )
        self.write(self.proof_path, proof)
        result = self.verify()
        self.assertTrue(result["valid"])
        self.assertEqual(result["local_receipts_verified"], 0)
        self.assertEqual(result["local_artifact_arms_verified"], 0)
        self.assertEqual(result["retained_4096_audits_recomputed"], 0)
        self.assertFalse(result["local_artifacts_verified"])
        with self.assertRaisesRegex(ValueError, "required but absent"):
            self.verify(require_local=True)

    def test_raw_receipt_field_mutation_and_file_hash_tamper_are_rejected(self) -> None:
        proof = self.load()
        arm = proof["arms"][0]
        receipt_path = Path(arm["receipt"]["path"])
        receipt, receipt_bytes = common._read_json(receipt_path, "test.receipt")
        receipt["spec_s"] = 1.0
        with self.assertRaisesRegex(ValueError, r"receipt\.spec_s"):
            verifier._validate_receipt(receipt, receipt_bytes, arm, 0)

        tampered_receipt = self.root / "tampered-receipt.json"
        tampered_receipt.write_bytes(receipt_path.read_bytes() + b" ")
        proof["arms"][0]["receipt"]["path"] = str(tampered_receipt)
        self.write(self.proof_path, proof)
        with self.assertRaisesRegex(ValueError, r"receipt\.(bytes|sha256)"):
            self.verify()

    def test_local_png_tamper_is_rejected(self) -> None:
        proof = self.load()
        arm = proof["arms"][0]
        source_root = Path(arm["local_artifacts"]["root"])
        relative = PurePosixPath(arm["local_artifacts"]["unit_relative_path"])
        target_root = self.root / "artifacts"
        target_unit = target_root / Path(*relative.parts)
        target_unit.mkdir(parents=True)
        for name in verifier.FILE_NAMES:
            shutil.copy2(
                source_root / Path(*relative.parts) / name,
                target_unit / name,
            )
        arm["local_artifacts"]["root"] = str(target_root)
        self.write(self.proof_path, proof)
        self.assertTrue(self.verify(require_local=True)["local_artifacts_verified"])

        draft = target_unit / "draft.png"
        data = bytearray(draft.read_bytes())
        data[-1] ^= 1
        draft.write_bytes(data)
        with self.assertRaisesRegex(ValueError, r"draft\.png\.sha256"):
            self.verify(require_local=True)

    def test_cli_emits_closed_success_and_error_envelopes(self) -> None:
        success = subprocess.run(
            [
                sys.executable,
                str(HERE / "verify_render_bmw_integrator_screen.py"),
                "--require-local-artifacts",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(success.returncode, 0, success.stderr)
        result = json.loads(success.stdout)
        self.assertTrue(result["valid"])
        self.assertFalse(result["meets_50x"])

        failure = subprocess.run(
            [
                sys.executable,
                str(HERE / "verify_render_bmw_integrator_screen.py"),
                "--proof",
                str(self.root / "missing.json"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(failure.returncode, 2)
        envelope = json.loads(failure.stderr)
        self.assertFalse(envelope["valid"])
        self.assertEqual(envelope["kind"], verifier.RESULT_KIND)


if __name__ == "__main__":
    unittest.main()
