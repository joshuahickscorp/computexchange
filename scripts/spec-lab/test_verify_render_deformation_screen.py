#!/usr/bin/env python3
"""Mutation tests for the Stylized Levi deformation-screen verifier."""

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

import verify_render_deformation_screen as verifier  # noqa: E402
import verify_render_transfer_matrix as common  # noqa: E402


class DeformationScreenVerifierTest(unittest.TestCase):
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

    def test_current_proof_and_all_local_artifacts_pass_as_negative_screen(
        self,
    ) -> None:
        result = verifier.verify(require_local_artifacts=True)
        self.assertTrue(result["valid"])
        self.assertTrue(result["local_artifacts_verified"])
        self.assertEqual(result["source_files_verified"], 4)
        self.assertEqual(result["local_receipts_verified"], 5)
        self.assertEqual(result["local_artifact_runs_verified"], 5)
        self.assertEqual(result["local_artifact_files_verified"], 35)
        self.assertTrue(result["deformation_change_recomputed"])
        self.assertEqual(result["projected_4096_baseline_s"], 29.886616)
        self.assertEqual(result["projected_speedup_x"], 35.69766)
        self.assertFalse(result["projection_is_measured"])
        self.assertFalse(result["meets_50x"])
        self.assertFalse(result["fresh_4096_run_authorized"])
        self.assertFalse(result["production_ready"])

    def test_duplicate_keys_and_nonfinite_numbers_are_rejected(self) -> None:
        self.proof_path.write_text(
            '{"schema_version":2,"schema_version":2}\n', encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "duplicate key"):
            self.verify()

        raw = verifier.DEFAULT_PROOF.read_text(encoding="utf-8")
        self.proof_path.write_text(
            raw.replace(
                '"projected_speedup_x": 35.69766',
                '"projected_speedup_x": NaN',
                1,
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "non-finite"):
            self.verify()

    def test_projection_arithmetic_mutation_is_rejected(self) -> None:
        proof = self.load()
        proof["slope_screen"]["projected_baseline_s"] = 31.0
        with self.assertRaisesRegex(ValueError, "projected_baseline_s"):
            verifier._validate_semantics(proof)

        proof = self.load()
        proof["slope_screen"]["projected_speedup_x"] = 40.0
        with self.assertRaisesRegex(ValueError, "projected_speedup_x"):
            verifier._validate_semantics(proof)

    def test_coherent_fake_50x_promotion_is_rejected(self) -> None:
        proof = self.load()
        run = proof["runs"][3]
        run["product_s"] = 0.5
        run["measured_speedup_x"] = round(run["baseline_s"] / 0.5, 6)
        slope = proof["slope_screen"]
        slope["product_s"] = 0.5
        slope["projected_speedup_x"] = round(
            slope["projected_baseline_s"] / 0.5, 6
        )
        slope["meets_50x"] = True
        with self.assertRaisesRegex(ValueError, "cannot be promoted to 50x"):
            verifier._validate_semantics(proof)

    def test_portable_proof_allows_absent_cache_but_required_mode_fails(
        self,
    ) -> None:
        proof = self.load()
        for key in ("archive", "original_scene", "derivative"):
            proof["source"][key]["path"] = str(
                self.root / f"absent-{key}"
            )
        for index, run in enumerate(proof["runs"]):
            run["receipt"]["path"] = str(
                self.root / f"absent-receipt-{index}.json"
            )
            run["local_artifacts"]["root"] = str(
                self.root / f"absent-artifacts-{index}"
            )
        self.write(self.proof_path, proof)
        result = self.verify()
        self.assertTrue(result["valid"])
        self.assertEqual(result["source_files_verified"], 1)
        self.assertEqual(result["local_receipts_verified"], 0)
        self.assertEqual(result["local_artifact_runs_verified"], 0)
        self.assertFalse(result["deformation_change_recomputed"])
        self.assertFalse(result["local_artifacts_verified"])
        with self.assertRaisesRegex(ValueError, "required local file is absent"):
            self.verify(require_local=True)

    def test_receipt_and_png_tamper_are_rejected(self) -> None:
        proof = self.load()
        source_receipt = Path(proof["runs"][0]["receipt"]["path"])
        tampered_receipt = self.root / "tampered-receipt.json"
        tampered_receipt.write_bytes(source_receipt.read_bytes() + b" ")
        proof["runs"][0]["receipt"]["path"] = str(tampered_receipt)
        self.write(self.proof_path, proof)
        with self.assertRaisesRegex(ValueError, r"receipt\.(bytes|sha256)"):
            self.verify()

        proof = json.loads(verifier.DEFAULT_PROOF.read_text(encoding="utf-8"))
        run = proof["runs"][0]
        source_root = Path(run["local_artifacts"]["root"])
        relative = PurePosixPath(run["local_artifacts"]["unit_relative_path"])
        target_root = self.root / "artifacts"
        target_unit = target_root / Path(*relative.parts)
        target_unit.mkdir(parents=True)
        for name in verifier.FILE_NAMES:
            shutil.copy2(
                source_root / Path(*relative.parts) / name,
                target_unit / name,
            )
        run["local_artifacts"]["root"] = str(target_root)
        self.write(self.proof_path, proof)
        self.assertTrue(self.verify(require_local=True)["local_artifacts_verified"])
        draft = target_unit / "draft.png"
        data = bytearray(draft.read_bytes())
        data[-1] ^= 1
        draft.write_bytes(data)
        with self.assertRaisesRegex(ValueError, r"draft\.png\.sha256"):
            self.verify(require_local=True)

    def test_schema_v2_artifacts_are_enforced(self) -> None:
        proof = self.load()
        run = proof["runs"][0]
        root = Path(run["local_artifacts"]["root"])
        unit = PurePosixPath(run["local_artifacts"]["unit_relative_path"])
        manifest_path = root / Path(*unit.parts) / "draft-manifest.json"
        manifest, _ = common._read_json(manifest_path, "test.manifest")
        manifest["schema_version"] = 1
        with self.assertRaisesRegex(ValueError, "schema_version"):
            verifier._validate_manifest(manifest, run, "draft", 0)

    def test_cli_emits_closed_success_and_error_envelopes(self) -> None:
        success = subprocess.run(
            [
                sys.executable,
                str(HERE / "verify_render_deformation_screen.py"),
                "--require-local-artifacts",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(success.returncode, 0, success.stderr)
        result = json.loads(success.stdout)
        self.assertTrue(result["valid"])
        self.assertFalse(result["projection_is_measured"])
        self.assertFalse(result["meets_50x"])

        failure = subprocess.run(
            [
                sys.executable,
                str(HERE / "verify_render_deformation_screen.py"),
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
        self.assertEqual(envelope["schema_version"], 2)
        self.assertEqual(envelope["kind"], verifier.RESULT_KIND)


if __name__ == "__main__":
    unittest.main()
