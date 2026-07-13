#!/usr/bin/env python3
"""Mutation tests for the local render transfer-matrix verifier."""

from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
PROOF_ROOT = ROOT / "proof" / "performance"
MATRIX_SOURCE = (
    PROOF_ROOT / "apple-metal-render-transfer-matrix-2026-07-12.json"
)
sys.path.insert(0, str(HERE))

import verify_render_transfer_matrix as verifier  # noqa: E402


def _json_bytes(value: object, *, allow_nan: bool = False) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=allow_nan,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _write_json(path: Path, value: object, *, allow_nan: bool = False) -> None:
    path.write_bytes(_json_bytes(value, allow_nan=allow_nan))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TransferMatrixFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.matrix = json.loads(MATRIX_SOURCE.read_text(encoding="utf-8"))
        self.matrix_path = root / "matrix.json"
        for row in self.matrix["scenes"]:
            source = PROOF_ROOT / row["receipt"]["path"]
            receipt = json.loads(source.read_text(encoding="utf-8"))
            receipt["artifact_root"] = str(root / "absent-artifacts" / row["key"])
            receipt["scene"] = str(root / "absent-scenes" / f"{row['key']}.blend")
            target = root / row["receipt"]["path"]
            _write_json(target, receipt)
            row["receipt"]["sha256"] = _sha256(target)
        self.save_matrix()

    def save_matrix(self, *, allow_nan: bool = False) -> None:
        _write_json(self.matrix_path, self.matrix, allow_nan=allow_nan)

    def row(self, key: str) -> dict[str, object]:
        return next(row for row in self.matrix["scenes"] if row["key"] == key)

    def receipt_path(self, key: str) -> Path:
        return self.root / self.row(key)["receipt"]["path"]

    def receipt(self, key: str) -> dict[str, object]:
        return json.loads(self.receipt_path(key).read_text(encoding="utf-8"))

    def save_receipt(self, key: str, receipt: dict[str, object]) -> None:
        path = self.receipt_path(key)
        _write_json(path, receipt)
        self.row(key)["receipt"]["sha256"] = _sha256(path)
        self.save_matrix()

    def verify(self) -> dict[str, object]:
        return verifier.verify_transfer_matrix(
            self.matrix_path, receipts_root=self.root
        )

    def install_local_product_evidence(self, key: str) -> Path:
        row = self.row(key)
        receipt = self.receipt(key)
        artifact_root = self.root / "local-artifacts" / key
        receipt["artifact_root"] = str(artifact_root)
        output_path = receipt["outputs"][0]["manifest_path"]
        draft_manifest_path = artifact_root / output_path
        unit_dir = draft_manifest_path.parent
        unit_dir.mkdir(parents=True)

        draft_png_path = unit_dir / "draft.png"
        verify_png_path = unit_dir / "verify.png"
        draft_png_path.write_bytes(b"fixture draft PNG bytes")
        verify_png_path.write_bytes(b"fixture independent verify PNG bytes")
        draft_png_relative = draft_png_path.relative_to(artifact_root).as_posix()
        verify_png_relative = verify_png_path.relative_to(artifact_root).as_posix()
        draft_sha = _sha256(draft_png_path)
        verify_sha = _sha256(verify_png_path)
        receipt["benchmark_audit"]["candidate"]["artifact_sha256"] = draft_sha

        if key == "pavilion":
            bundle = row["sanitized_derivative"]["bundle"]
        else:
            bundle = row["bundle"]
        binding = receipt["benchmark_audit"]["binding_sha256"]
        seed = 123456
        draft_manifest = {
            "schema_version": 1,
            "kind": "cx_cycles_preview_manifest",
            "phase": "draft",
            "binding_sha256": binding,
            "artifact": {
                "path": draft_png_relative,
                "sha256": draft_sha,
                "media_type": "image/png",
            },
            "render": {
                "width": receipt["resolution"][0],
                "height": receipt["resolution"][1],
                "frame": receipt["frame"],
                "samples": receipt["draft_samples"],
                "sample_offset": receipt["sample_ranges"]["draft"][0],
                "device": receipt["device"],
                "engine": "CYCLES",
                "seed": seed,
                "worker_renderer_identity": receipt["worker_renderer_identity"],
            },
            "scene": {
                "sha256": receipt["scene_sha256"],
                "relative_path": f"{key}.blend",
                "bundle_files": bundle["files"],
                "bundle_bytes": bundle["bytes"],
                "bundle_sha256": bundle["sha256"],
            },
            "pins": {
                "backend_sha256": receipt["pins"]["backend_sha256"],
                "blender_sha256": receipt["pins"]["blender_sha256"],
                "child_script_sha256": "a" * 64,
                "controller_adapter_sha256": receipt["pins"][
                    "controller_adapter_sha256"
                ],
                "controller_core_sha256": receipt["pins"][
                    "controller_core_sha256"
                ],
            },
            "preview_only": True,
            "production_ready": False,
            "artifact_verified": False,
            "billing_eligible": False,
            "evidence": "synthetic",
            "execution_identity_revalidation": {
                "initial_content": "sha256",
                "per_render": "fixture",
            },
            "unit_id": "local-metal-benchmark",
        }
        _write_json(draft_manifest_path, draft_manifest)

        gate = row["product_gate"]
        audit = receipt["benchmark_audit"]
        verification_manifest = {
            "schema_version": 1,
            "kind": "cx_cycles_preview_verification",
            "accepted": True,
            "artifact_verified": False,
            "billing_eligible": False,
            "binding_sha256": binding,
            "draft_sample_offset": receipt["sample_ranges"]["draft"][0],
            "draft_seed": seed,
            "evidence": "synthetic",
            "failing_tile_count": 0,
            "failing_tiles": [],
            "failing_tiles_truncated": False,
            "global_agreement": gate["global_agreement"],
            "global_min": 0.9,
            "independent_seed": True,
            "metric": "one_minus_mean_absolute_rgb_difference",
            "microtile_contract": "fixed_scale_catastrophic_defect_sentinel",
            "microtile_count": audit["microtile_count"],
            "microtile_grid": audit["microtile_grid"],
            "preview_only": True,
            "production_ready": False,
            "repair_plan": None,
            "sample_ranges_disjoint": True,
            "selected_manifest_path": output_path,
            "tile_contract": "resolution_relative_regions_not_fixed_pixel_defects",
            "tile_count": audit["tile_count"],
            "tile_grid": audit["tile_grid"],
            "unit_id": "local-metal-benchmark",
            "verify_artifact": {
                "path": verify_png_relative,
                "sha256": verify_sha,
            },
            "verify_sample_offset": receipt["sample_ranges"]["verify"][0],
            "verify_seed": seed + 1,
            "worst_microtile_agreement": gate["worst_microtile_agreement"],
            "worst_microtile_min": 0.7,
            "worst_tile_agreement": gate["worst_regional_agreement"],
            "worst_tile_min": 0.85,
        }
        _write_json(unit_dir / "verification-manifest.json", verification_manifest)
        self.save_receipt(key, receipt)
        return unit_dir / "verification-manifest.json"


class RenderTransferMatrixVerifierTest(unittest.TestCase):
    def test_current_proof_passes_and_discloses_v1_binding_limits(self) -> None:
        result = verifier.verify_transfer_matrix(MATRIX_SOURCE)
        self.assertTrue(result["ok"])
        self.assertEqual(result["receipts"]["count"], 3)
        self.assertFalse(
            result["local_corroboration"]["product_exact_metrics_receipt_bound"]
        )
        self.assertFalse(
            result["local_corroboration"]["pavilion_source_and_probe_receipt_bound"]
        )
        self.assertTrue(any("not hashed" in row for row in result["limitations"]))

    def test_portable_receipt_only_fixture_passes_without_local_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = TransferMatrixFixture(Path(raw))
            result = fixture.verify()
            self.assertTrue(result["ok"])
            self.assertEqual(
                result["local_corroboration"][
                    "product_verification_manifests_checked"
                ],
                0,
            )
            self.assertFalse(
                result["local_corroboration"][
                    "product_verification_manifests_available"
                ]
            )

    def test_receipt_hash_and_unsafe_path_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = TransferMatrixFixture(Path(raw))
            receipt_path = fixture.receipt_path("bmw27")
            receipt_path.write_bytes(receipt_path.read_bytes() + b" ")
            with self.assertRaisesRegex(
                verifier.MatrixValidationError, "receipt.sha256"
            ):
                fixture.verify()

        with tempfile.TemporaryDirectory() as raw:
            fixture = TransferMatrixFixture(Path(raw))
            fixture.row("classroom")["receipt"]["path"] = "../escape.json"
            fixture.save_matrix()
            with self.assertRaisesRegex(
                verifier.MatrixValidationError, "parent components"
            ):
                fixture.verify()

    def test_symlinked_receipt_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = TransferMatrixFixture(Path(raw))
            target = fixture.receipt_path("classroom")
            link = fixture.root / "linked-receipt.json"
            try:
                link.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            fixture.row("classroom")["receipt"] = {
                "path": link.name,
                "sha256": _sha256(target),
            }
            fixture.save_matrix()
            with self.assertRaisesRegex(
                verifier.MatrixValidationError, "symlinks"
            ):
                fixture.verify()

    def test_duplicate_key_and_nonfinite_number_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = TransferMatrixFixture(Path(raw))
            text = fixture.matrix_path.read_text(encoding="utf-8")
            text = text.replace(
                '"schema_version": 1,',
                '"schema_version": 1,\n  "schema_version": 1,',
                1,
            )
            fixture.matrix_path.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(
                verifier.MatrixValidationError, "duplicate key"
            ):
                fixture.verify()

        with tempfile.TemporaryDirectory() as raw:
            fixture = TransferMatrixFixture(Path(raw))
            fixture.matrix["summary"]["minimum_measured_speedup_x"] = float(
                "inf"
            )
            fixture.save_matrix(allow_nan=True)
            with self.assertRaisesRegex(
                verifier.MatrixValidationError, "non-finite"
            ):
                fixture.verify()

    def test_shared_pin_mutation_is_rejected_even_with_fresh_receipt_hash(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = TransferMatrixFixture(Path(raw))
            receipt = fixture.receipt("pavilion")
            receipt["pins"]["backend_sha256"] = "0" * 64
            fixture.save_receipt("pavilion", receipt)
            with self.assertRaisesRegex(
                verifier.MatrixValidationError, "shared_execution.pins"
            ):
                fixture.verify()

    def test_sample_range_and_summary_mutations_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = TransferMatrixFixture(Path(raw))
            receipt = fixture.receipt("bmw27")
            receipt["sample_ranges"]["verify"] = [31, 63]
            receipt["benchmark_audit"]["sample_ranges"]["verify"] = [31, 63]
            fixture.save_receipt("bmw27", receipt)
            with self.assertRaisesRegex(
                verifier.MatrixValidationError, "overlap"
            ):
                fixture.verify()

        with tempfile.TemporaryDirectory() as raw:
            fixture = TransferMatrixFixture(Path(raw))
            fixture.matrix["summary"][
                "scenes_meeting_50x_preview_experiment"
            ] = 3
            fixture.save_matrix()
            with self.assertRaisesRegex(
                verifier.MatrixValidationError, "matrix.summary"
            ):
                fixture.verify()

    def test_product_gate_threshold_and_speedup_mutations_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = TransferMatrixFixture(Path(raw))
            gate = fixture.row("classroom")["product_gate"]
            gate["worst_regional_agreement"] = 0.849999999
            fixture.save_matrix()
            with self.assertRaisesRegex(
                verifier.MatrixValidationError, "product_gate.accepted"
            ):
                fixture.verify()

        with tempfile.TemporaryDirectory() as raw:
            fixture = TransferMatrixFixture(Path(raw))
            receipt = fixture.receipt("bmw27")
            receipt["speedup_x"] += 0.000001
            receipt["controller_receipt"]["speedup_vs_baseline"] = receipt[
                "speedup_x"
            ]
            fixture.save_receipt("bmw27", receipt)
            with self.assertRaisesRegex(
                verifier.MatrixValidationError, "speedup_x"
            ):
                fixture.verify()

    def test_pavilion_provenance_mutation_is_rejected_as_unbound_policy_data(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = TransferMatrixFixture(Path(raw))
            fixture.row("pavilion")["source"]["archive_sha256"] = "1" * 64
            fixture.save_matrix()
            with self.assertRaisesRegex(
                verifier.MatrixValidationError, "source.archive_sha256"
            ):
                fixture.verify()

    def test_local_product_manifest_is_checked_and_binding_mutation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = TransferMatrixFixture(Path(raw))
            verification_path = fixture.install_local_product_evidence("classroom")
            result = fixture.verify()
            self.assertEqual(
                result["local_corroboration"][
                    "product_verification_manifests_checked"
                ],
                1,
            )

            manifest = json.loads(verification_path.read_text(encoding="utf-8"))
            manifest["binding_sha256"] = "f" * 64
            _write_json(verification_path, manifest)
            with self.assertRaisesRegex(
                verifier.MatrixValidationError,
                "verification_manifest.binding_sha256",
            ):
                fixture.verify()

    def test_cli_emits_json_success_and_error_envelopes(self) -> None:
        success = io.StringIO()
        with redirect_stdout(success):
            return_code = verifier.main(["--matrix", str(MATRIX_SOURCE)])
        self.assertEqual(return_code, 0)
        self.assertTrue(json.loads(success.getvalue())["ok"])

        failure = io.StringIO()
        with redirect_stdout(failure):
            return_code = verifier.main(["--matrix", "/definitely/absent.json"])
        self.assertEqual(return_code, 1)
        envelope = json.loads(failure.getvalue())
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], "validation_error")


if __name__ == "__main__":
    unittest.main()
