#!/usr/bin/env python3
"""Mutation tests for the Fishy Cat transfer-proof verifier."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import verify_render_hair_transfer as verifier  # noqa: E402


class HairTransferVerifierTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.receipt_path = self.root / "receipt.json"
        self.provenance_path = self.root / "provenance.json"
        shutil.copy2(verifier.DEFAULT_RECEIPT, self.receipt_path)
        shutil.copy2(verifier.DEFAULT_PROVENANCE, self.provenance_path)

    def load_receipt(self) -> dict[str, object]:
        return json.loads(self.receipt_path.read_text(encoding="utf-8"))

    def load_provenance(self) -> dict[str, object]:
        return json.loads(self.provenance_path.read_text(encoding="utf-8"))

    @staticmethod
    def write(path: Path, value: dict[str, object]) -> None:
        path.write_text(
            json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )

    def refresh_receipt_pin(self) -> None:
        receipt = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        provenance = self.load_provenance()
        raw = self.receipt_path.read_bytes()
        provenance["receipt"]["sha256"] = hashlib.sha256(raw).hexdigest()
        provenance["receipt"]["canonical_json_sha256"] = hashlib.sha256(
            verifier._canonical_bytes(receipt)
        ).hexdigest()
        self.write(self.provenance_path, provenance)

    def verify(self, *, require_local: bool = False) -> dict[str, object]:
        return verifier.verify(
            self.receipt_path,
            self.provenance_path,
            require_local_artifacts=require_local,
        )

    def test_current_proof_and_local_artifacts_pass(self) -> None:
        result = verifier.verify(require_local_artifacts=True)
        self.assertTrue(result["valid"])
        self.assertTrue(result["local_artifacts_verified"])
        self.assertEqual(result["speedup_x"], 54.541695)
        self.assertEqual(result["hair_particles_audited"], 13355)
        self.assertFalse(result["production_ready"])

    def test_speedup_and_threshold_mutations_fail_after_repin(self) -> None:
        receipt = self.load_receipt()
        receipt["speedup_x"] = 100.0
        receipt["controller_receipt"]["speedup_vs_baseline"] = 100.0
        self.write(self.receipt_path, receipt)
        self.refresh_receipt_pin()
        with self.assertRaisesRegex(ValueError, "receipt.speedup_x"):
            self.verify()

        receipt = json.loads(verifier.DEFAULT_RECEIPT.read_text(encoding="utf-8"))
        receipt["benchmark_audit"]["worst_microtile_agreement"] = 0.69
        receipt["benchmark_audit"]["passed"] = False
        self.write(self.receipt_path, receipt)
        self.refresh_receipt_pin()
        with self.assertRaisesRegex(ValueError, "reference audit did not pass"):
            self.verify()

    def test_duplicate_key_and_nonfinite_number_are_rejected(self) -> None:
        self.receipt_path.write_text(
            '{"schema_version":1,"schema_version":1}\n', encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "duplicate key"):
            self.verify()

        raw = verifier.DEFAULT_RECEIPT.read_text(encoding="utf-8")
        self.receipt_path.write_text(
            raw.replace('"baseline_s": 92.931794', '"baseline_s": NaN', 1),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "non-finite"):
            self.verify()

    def test_portable_receipt_passes_without_local_artifacts_unless_required(self) -> None:
        receipt = self.load_receipt()
        receipt["artifact_root"] = str(self.root / "absent-artifacts")
        self.write(self.receipt_path, receipt)
        self.refresh_receipt_pin()
        result = self.verify()
        self.assertFalse(result["local_artifacts_verified"])
        with self.assertRaisesRegex(ValueError, "required but absent"):
            self.verify(require_local=True)

    def test_local_png_tamper_is_rejected(self) -> None:
        receipt = self.load_receipt()
        source_root = Path(receipt["artifact_root"])
        local_root = self.root / "artifacts"
        referenced = [
            receipt["outputs"][0]["manifest_path"],
            receipt["benchmark_audit"]["baseline"]["manifest_path"],
        ]
        unit = PurePosixPath(referenced[0]).parent
        referenced.extend(
            str(unit / name)
            for name in (
                "verification-manifest.json",
                "benchmark-audit.json",
                "draft.png",
                "verify.png",
                "baseline.png",
            )
        )
        for raw in referenced:
            source = source_root / Path(*PurePosixPath(raw).parts)
            target = local_root / Path(*PurePosixPath(raw).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        receipt["artifact_root"] = str(local_root)
        self.write(self.receipt_path, receipt)
        self.refresh_receipt_pin()
        self.assertTrue(self.verify(require_local=True)["local_artifacts_verified"])

        draft = local_root / Path(*PurePosixPath(str(unit / "draft.png")).parts)
        data = bytearray(draft.read_bytes())
        data[-1] ^= 1
        draft.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "artifact.sha256"):
            self.verify(require_local=True)

    def test_cli_emits_closed_success_and_error_envelopes(self) -> None:
        success = subprocess.run(
            [sys.executable, str(HERE / "verify_render_hair_transfer.py")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertTrue(json.loads(success.stdout)["valid"])

        failure = subprocess.run(
            [
                sys.executable,
                str(HERE / "verify_render_hair_transfer.py"),
                "--receipt",
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
