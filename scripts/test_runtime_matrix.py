#!/usr/bin/env python3
"""Contract tests for the canonical runtime-matrix generator."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import runtime_matrix  # noqa: E402


SOURCE = ROOT / "proto" / "runtime-matrix.source.json"


class RuntimeMatrixTest(unittest.TestCase):
    def setUp(self):
        self.source = runtime_matrix.load_source(SOURCE)

    def assert_invalid(self, source, pattern):
        with self.assertRaisesRegex(runtime_matrix.MatrixValidationError, pattern):
            runtime_matrix.build_matrix(source)

    def test_generation_is_deterministic_and_source_order_independent(self):
        first = runtime_matrix.render_outputs(runtime_matrix.build_matrix(self.source))
        reordered = copy.deepcopy(self.source)
        for key in ("jobs", "models", "runtimes", "cells", "phase2_blockers"):
            reordered[key].reverse()
        for runtime in reordered["runtimes"]:
            runtime["hardware_classes"].reverse()
        for cell in reordered["cells"]:
            cell["evidence"].reverse()
        second = runtime_matrix.render_outputs(runtime_matrix.build_matrix(reordered))
        self.assertEqual(first, second)

    def test_duplicate_ids_are_rejected_for_every_entity(self):
        for key in ("jobs", "models", "runtimes", "cells"):
            with self.subTest(key=key):
                source = copy.deepcopy(self.source)
                source[key].append(copy.deepcopy(source[key][0]))
                self.assert_invalid(source, rf"duplicate {key} id")

    def test_duplicate_capability_tuple_is_rejected(self):
        source = copy.deepcopy(self.source)
        duplicate = copy.deepcopy(source["cells"][0])
        duplicate["id"] = "different-id-same-tuple"
        source["cells"].append(duplicate)
        self.assert_invalid(source, "duplicate capability tuple")

    def test_unknown_runtime_job_and_model_references_are_rejected(self):
        for field, value in (
            ("runtime", "missing_runtime"),
            ("job", "missing_job"),
            ("model", "missing-model"),
        ):
            with self.subTest(field=field):
                source = copy.deepcopy(self.source)
                source["cells"][0][field] = value
                self.assert_invalid(source, rf"references unknown {field}")

    def test_unknown_lifecycle_is_rejected(self):
        source = copy.deepcopy(self.source)
        source["cells"][0]["lifecycle"] = "wishful_thinking"
        self.assert_invalid(source, "unknown lifecycle")

    def test_model_wire_kind_is_closed_and_derived_into_every_cell(self):
        source = copy.deepcopy(self.source)
        source["models"][0]["wire_kind"] = "safetensors"
        self.assert_invalid(source, "unsupported agent wire value")

        matrix = runtime_matrix.build_matrix(self.source)
        model_kinds = {row["id"]: row["wire_kind"] for row in matrix["models"]}
        for cell in matrix["cells"]:
            expected = model_kinds[cell["model"]] if cell["model"] is not None else None
            self.assertEqual(cell["model_kind"], expected, cell["id"])
        advertised = {
            row["cell_id"]: row for row in matrix["advertised_projection"]["tuples"]
        }
        self.assertEqual(advertised["candle-metal-minilm-embed"]["model_kind"], "hf")
        self.assertEqual(advertised["candle-metal-llama1-infer"]["model_kind"], "gguf")

        outputs = runtime_matrix.render_outputs(matrix)
        self.assertIn(b'ModelKind:       "hf"', outputs["control/runtime_matrix_generated.go"])
        self.assertIn(b'model_kind: Some("hf")', outputs["agent/src/runtime_matrix_generated.rs"])

    def test_production_cell_requires_production_dependencies(self):
        for entity, field in (
            ("runtimes", "runtime"),
            ("jobs", "job"),
            ("models", "model"),
        ):
            with self.subTest(entity=entity):
                source = copy.deepcopy(self.source)
                target_id = source["cells"][0][field]
                target = next(row for row in source[entity] if row["id"] == target_id)
                target["lifecycle"] = "hardware_pending"
                singular = entity[:-1]
                self.assert_invalid(source, rf"non-production {singular}")

    def test_production_cell_requires_model_runner_verification_and_evidence(self):
        mutations = (
            ("model", None, "requires a model reference"),
            ("runner", None, "has no runner"),
            ("verification", "none", "has no verification"),
            ("evidence", [], "has no evidence"),
        )
        for field, value, pattern in mutations:
            with self.subTest(field=field):
                source = copy.deepcopy(self.source)
                source["cells"][0][field] = value
                self.assert_invalid(source, pattern)

    def test_model_required_wire_placeholder_cannot_be_promoted_without_model(self):
        source = copy.deepcopy(self.source)
        cell = next(row for row in source["cells"] if row["id"] == "wire-image-gen")
        cell["lifecycle"] = "stub"
        self.assert_invalid(source, "requires a model reference")

    def test_only_production_cells_enter_advertised_projection(self):
        matrix = runtime_matrix.build_matrix(self.source)
        production_ids = {
            row["id"] for row in matrix["cells"] if row["lifecycle"] == "production"
        }
        advertised_ids = set(matrix["advertised_projection"]["cell_ids"])
        tuple_ids = {row["cell_id"] for row in matrix["advertised_projection"]["tuples"]}
        self.assertEqual(advertised_ids, production_ids)
        self.assertEqual(tuple_ids, production_ids)
        self.assertTrue(matrix["advertised_projection"]["tuples"])
        self.assertTrue(
            all(
                next(cell for cell in matrix["cells"] if cell["id"] == row["cell_id"])[
                    "lifecycle"
                ]
                == "production"
                for row in matrix["advertised_projection"]["tuples"]
            )
        )

    def test_stale_check_detects_change_without_rewriting(self):
        outputs = runtime_matrix.render_outputs(runtime_matrix.build_matrix(self.source))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_matrix.write_outputs(root, outputs)
            self.assertEqual(runtime_matrix.stale_outputs(root, outputs), [])
            stale_path = root / "docs" / "RUNTIME_MATRIX.md"
            stale_path.write_text("stale\n", encoding="utf-8")
            self.assertEqual(
                runtime_matrix.stale_outputs(root, outputs), ["docs/RUNTIME_MATRIX.md"]
            )
            self.assertEqual(stale_path.read_text(encoding="utf-8"), "stale\n")

    def test_cli_writes_checkable_outputs_and_machine_readable_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "out"
            artifacts = Path(tmp) / "artifacts"
            self.assertEqual(
                runtime_matrix.main(
                    [
                        "--source",
                        str(SOURCE),
                        "--output-root",
                        str(root),
                        "--artifact-dir",
                        str(artifacts),
                    ]
                ),
                0,
            )
            self.assertEqual(
                runtime_matrix.main(
                    ["--source", str(SOURCE), "--output-root", str(root), "--check"]
                ),
                0,
            )
            matrix = json.loads((artifacts / "matrix.json").read_text(encoding="utf-8"))
            report = json.loads((artifacts / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "NOT_PROVEN")
            self.assertFalse(report["gate_proven"])
            self.assertEqual(report["phase"], "tranche_4_bound_execution_authority")
            self.assertTrue(report["agent_advertisement_consumes_generated_projection"])
            self.assertTrue(report["dispatch_and_receipts_bind_exact_cells"])
            self.assertTrue(report["dispatch_carries_generated_model_kind"])
            self.assertTrue(report["production_consumes_generated_projection"])
            self.assertTrue(report["registration_persists_exact_worker_cells"])
            self.assertTrue(report["scheduler_requires_exact_worker_cells"])
            self.assertFalse(report["legacy_array_workers_backfilled"])
            self.assertEqual(report["matrix_sha256"], matrix["matrix_sha256"])


if __name__ == "__main__":
    unittest.main()
