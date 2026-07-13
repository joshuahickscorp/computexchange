#!/usr/bin/env python3
"""Mutation tests for the held-out Koro portrait transfer verifier."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import verify_render_koro_transfer as verifier  # noqa: E402


class KoroTransferVerifierTest(unittest.TestCase):
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

    def cache_paths(self) -> set[Path]:
        receipt = self.load_receipt()
        provenance = self.load_provenance()
        paths = {
            Path(receipt["artifact_root"]),
            Path(provenance["source"]["archive_path"]),
            Path(provenance["source"]["bundle_root"]),
            Path(
                provenance["selection_protocol"]["full_resolution_calibration"][
                    "receipt_path"
                ]
            ),
        }
        paths.update(
            Path(row["receipt_path"])
            for row in provenance["selection_protocol"][
                "low_resolution_screening_runs"
            ]
        )
        return paths

    def hide_caches(self) -> mock._patch:
        hidden = self.cache_paths()
        original_exists = Path.exists

        def exists(path: Path) -> bool:
            return False if path in hidden else original_exists(path)

        return mock.patch.object(Path, "exists", exists)

    def test_current_proof_and_all_local_corroboration_pass(self) -> None:
        result = verifier.verify(require_local_artifacts=True)
        self.assertTrue(result["valid"])
        self.assertEqual(result["speedup_x"], 84.250743)
        self.assertTrue(result["source_bundle_verified"])
        self.assertEqual(result["calibration_receipts_verified"], 5)
        self.assertTrue(result["local_artifacts_verified"])
        self.assertTrue(result["product_gate_recomputed"])
        self.assertTrue(result["reference_audit_recomputed"])
        self.assertEqual(result["stored_hair_particles_audited"], 1596)
        self.assertEqual(result["configured_viewport_children_audited"], 226080)
        self.assertEqual(result["configured_render_children_audited"], 806530)
        self.assertFalse(result["production_ready"])

    def test_fake_100x_and_unpinned_receipt_mutations_fail(self) -> None:
        receipt = self.load_receipt()
        receipt["speedup_x"] = 100.0
        receipt["controller_receipt"]["speedup_vs_baseline"] = 100.0
        receipt["meets_100x_preview_experiment"] = True
        self.write(self.receipt_path, receipt)
        self.refresh_receipt_pin()
        with self.assertRaisesRegex(ValueError, "receipt.sha256"):
            self.verify()

        shutil.copy2(verifier.DEFAULT_RECEIPT, self.receipt_path)
        receipt = self.load_receipt()
        receipt["claim_scope"] += " tampered"
        self.write(self.receipt_path, receipt)
        self.refresh_receipt_pin()
        with self.assertRaisesRegex(ValueError, "receipt.sha256"):
            self.verify()

        changed_raw_sha = hashlib.sha256(self.receipt_path.read_bytes()).hexdigest()
        with mock.patch.object(verifier, "RECEIPT_RAW_SHA256", changed_raw_sha):
            with self.assertRaisesRegex(ValueError, "canonical_json_sha256"):
                self.verify()

    def test_duplicate_extra_key_and_nonfinite_are_rejected(self) -> None:
        self.receipt_path.write_text(
            '{"schema_version":1,"schema_version":1}\n', encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "duplicate key"):
            self.verify()

        shutil.copy2(verifier.DEFAULT_RECEIPT, self.receipt_path)
        raw = self.receipt_path.read_text(encoding="utf-8")
        self.receipt_path.write_text(
            raw.replace('"baseline_s": 112.396726', '"baseline_s": NaN', 1),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "non-finite"):
            self.verify()

        shutil.copy2(verifier.DEFAULT_RECEIPT, self.receipt_path)
        receipt = self.load_receipt()
        receipt["unexpected"] = True
        self.write(self.receipt_path, receipt)
        self.refresh_receipt_pin()
        with self.assertRaisesRegex(ValueError, "receipt.sha256"):
            self.verify()

    def test_portable_proof_passes_without_caches_unless_required(self) -> None:
        with self.hide_caches():
            result = self.verify()
            self.assertFalse(result["source_bundle_verified"])
            self.assertEqual(result["calibration_receipts_verified"], 0)
            self.assertFalse(result["local_artifacts_verified"])
            self.assertFalse(result["product_gate_recomputed"])
            with self.assertRaisesRegex(ValueError, "required"):
                self.verify(require_local=True)

    def test_source_bundle_tamper_is_rejected(self) -> None:
        provenance = self.load_provenance()
        source_root = Path(provenance["source"]["bundle_root"])
        copied = self.root / "source-bundle"
        shutil.copytree(source_root, copied)
        provenance["source"]["bundle_root"] = str(copied)
        self.write(self.provenance_path, provenance)

        background = copied / "background.png"
        data = bytearray(background.read_bytes())
        data[-1] ^= 1
        background.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "local.source.bundle.entries"):
            self.verify()

    def test_local_png_tamper_is_rejected(self) -> None:
        receipt = self.load_receipt()
        provenance = self.load_provenance()
        draft = (
            Path(receipt["artifact_root"])
            / provenance["local_artifacts"]["unit_relative_path"]
            / "draft.png"
        ).resolve()
        original = verifier.common._stable_bytes

        def tampered(
            path: Path,
            location: str,
            *,
            maximum: int = verifier.common.MAX_JSON_BYTES,
        ) -> bytes:
            data = original(path, location, maximum=maximum)
            if path == draft and location == "local.draft.png":
                changed = bytearray(data)
                changed[-1] ^= 1
                return bytes(changed)
            return data

        with mock.patch.object(verifier.common, "_stable_bytes", side_effect=tampered):
            with self.assertRaisesRegex(ValueError, "local.draft.png.sha256"):
                self.verify()

    def test_recomputed_product_gate_rejects_self_consistent_artifact_repin(self) -> None:
        receipt = self.load_receipt()
        provenance = self.load_provenance()
        unit_path = (
            Path(receipt["artifact_root"])
            / provenance["local_artifacts"]["unit_relative_path"]
        )
        draft_bytes = (unit_path / "draft.png").read_bytes()
        verification_path = unit_path / "verification-manifest.json"
        verification = json.loads(verification_path.read_text(encoding="utf-8"))
        verification["verify_artifact"]["sha256"] = hashlib.sha256(
            draft_bytes
        ).hexdigest()
        verification_bytes = (
            json.dumps(verification, sort_keys=True, indent=2, allow_nan=False) + "\n"
        ).encode("utf-8")
        provenance["local_artifacts"]["files"]["verify.png"] = {
            "bytes": len(draft_bytes),
            "sha256": hashlib.sha256(draft_bytes).hexdigest(),
        }
        provenance["local_artifacts"]["files"]["verification-manifest.json"] = {
            "bytes": len(verification_bytes),
            "sha256": hashlib.sha256(verification_bytes).hexdigest(),
        }
        self.write(self.provenance_path, provenance)
        original = verifier.common._stable_bytes
        verify_path = (unit_path / "verify.png").resolve()
        verification_resolved = verification_path.resolve()

        def repinned(
            path: Path,
            location: str,
            *,
            maximum: int = verifier.common.MAX_JSON_BYTES,
        ) -> bytes:
            if path == verify_path and location == "local.verify.png":
                return draft_bytes
            if path == verification_resolved and location == "local.verification-manifest.json":
                return verification_bytes
            return original(path, location, maximum=maximum)

        with mock.patch.object(verifier.common, "_stable_bytes", side_effect=repinned):
            with self.assertRaisesRegex(ValueError, "local.product_gate"):
                self.verify()

    def test_schema_downgrade_and_repair_or_cache_claims_fail(self) -> None:
        receipt = self.load_receipt()
        receipt["outputs"][0]["schema_version"] = 1
        self.write(self.receipt_path, receipt)
        self.refresh_receipt_pin()
        with self.assertRaisesRegex(ValueError, "receipt.sha256"):
            self.verify()

        shutil.copy2(verifier.DEFAULT_RECEIPT, self.receipt_path)
        receipt = self.load_receipt()
        receipt["cache_used"] = True
        self.write(self.receipt_path, receipt)
        self.refresh_receipt_pin()
        with self.assertRaisesRegex(ValueError, "receipt.sha256"):
            self.verify()

        shutil.copy2(verifier.DEFAULT_RECEIPT, self.receipt_path)
        receipt = self.load_receipt()
        receipt["controller_receipt"]["accepted_units"] = 0
        receipt["controller_receipt"]["repaired_units"] = 1
        self.write(self.receipt_path, receipt)
        self.refresh_receipt_pin()
        with self.assertRaisesRegex(ValueError, "receipt.sha256"):
            self.verify()

    def test_selection_separation_and_portrait_shape_are_enforced(self) -> None:
        provenance = self.load_provenance()
        provenance["selection_protocol"]["calibration_frames"].append(9)
        self.write(self.provenance_path, provenance)
        with self.assertRaisesRegex(ValueError, "calibration_frames"):
            self.verify()

        shutil.copy2(verifier.DEFAULT_PROVENANCE, self.provenance_path)
        receipt = self.load_receipt()
        receipt["resolution"] = [1920, 1080]
        self.write(self.receipt_path, receipt)
        self.refresh_receipt_pin()
        with self.assertRaisesRegex(ValueError, "receipt.sha256"):
            self.verify()

        shutil.copy2(verifier.DEFAULT_RECEIPT, self.receipt_path)
        shutil.copy2(verifier.DEFAULT_PROVENANCE, self.provenance_path)
        provenance = self.load_provenance()
        provenance["selection_protocol"]["selected_reason"] = "8+8 selected"
        self.write(self.provenance_path, provenance)
        with self.assertRaisesRegex(ValueError, "selected_reason"):
            self.verify()

        shutil.copy2(verifier.DEFAULT_PROVENANCE, self.provenance_path)
        provenance = self.load_provenance()
        provenance["selection_protocol"]["held_out_attestation"] = (
            "independently_proven"
        )
        self.write(self.provenance_path, provenance)
        with self.assertRaisesRegex(ValueError, "held_out_attestation"):
            self.verify()

    def test_v2_binding_is_recomputed_and_coherent_mutation_fails_portably(self) -> None:
        self.assertEqual(
            verifier._recompute_binding_sha256(), verifier.EXPECTED_BINDING_SHA256
        )
        receipt = self.load_receipt()
        receipt["benchmark_audit"]["binding_sha256"] = "f" * 64
        self.write(self.receipt_path, receipt)
        self.refresh_receipt_pin()
        with self.hide_caches():
            with self.assertRaisesRegex(ValueError, "receipt.sha256"):
                self.verify()
        with self.assertRaisesRegex(ValueError, "benchmark_audit.binding_sha256"):
            verifier._validate_receipt(receipt)

    def test_quality_policy_and_cache_scope_caveats_are_enforced(self) -> None:
        provenance = self.load_provenance()
        provenance["quality"]["receipt_quality_gate_spec_legacy_incomplete"] = (
            "g>=0.9,wt>=0.85 is the complete policy"
        )
        self.write(self.provenance_path, provenance)
        with self.assertRaisesRegex(ValueError, "legacy_incomplete"):
            self.verify()

        shutil.copy2(verifier.DEFAULT_PROVENANCE, self.provenance_path)
        provenance = self.load_provenance()
        provenance["execution"]["cache_used_false_scope"] = "all caches were cold"
        self.write(self.provenance_path, provenance)
        with self.assertRaisesRegex(ValueError, "cache_used_false_scope"):
            self.verify()

    def test_cli_emits_closed_success_and_error_envelopes(self) -> None:
        success = subprocess.run(
            [sys.executable, str(HERE / "verify_render_koro_transfer.py")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertTrue(json.loads(success.stdout)["valid"])

        failure = subprocess.run(
            [
                sys.executable,
                str(HERE / "verify_render_koro_transfer.py"),
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
