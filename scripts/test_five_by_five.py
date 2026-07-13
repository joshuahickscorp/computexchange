#!/usr/bin/env python3
"""Tests for honest 5/5 reporting and terminal attached-command envelopes."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).with_name("five-by-five.py")
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("five_by_five", SCRIPT)
assert SPEC and SPEC.loader
subject = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(subject)


def registry(gates: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "facets": [
            {
                "id": "demo",
                "target": 5,
                "definition_of_5": "Every demo outcome and prerequisite is proven.",
                "gates": gates,
            }
        ],
    }


def gate(identifier: str, state: str, **extra: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": identifier,
        "state": state,
        "scope": "local",
        "acceptance": f"{identifier} acceptance",
        "owner": "us",
    }
    if state != "proven":
        value["next_action"] = f"Implement and prove {identifier}."
    value.update(extra)
    return value


class FiveByFiveTest(unittest.TestCase):
    def write_registry(self, root: Path, value: dict[str, object]) -> Path:
        path = root / "registry.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_report_separates_prerequisites_from_outcomes_and_never_implies_partial_score(self):
        facets = registry(
            [
                gate("harness", "proven", role="prerequisite", command="true"),
                gate("physical", "external_pending"),
            ]
        )["facets"]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            subject.print_report(facets)
        rendered = output.getvalue()
        self.assertIn("5/5 NO", rendered)
        self.assertIn("outcomes proven 0/1", rendered)
        self.assertIn("prerequisites proven 1/1", rendered)
        self.assertIn("(prerequisite)", rendered)
        self.assertNotIn("target 5/5 | proven", rendered)

    def test_registry_rejects_unverifiable_proven_state_and_ambiguous_executors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unverifiable = self.write_registry(root, registry([gate("claim", "proven")]))
            with self.assertRaisesRegex(ValueError, "proven gate needs"):
                subject.load_registry(unverifiable)

            ambiguous = registry(
                [
                    gate(
                        "claim",
                        "proven",
                        command="true",
                        evidence_validator="true",
                    )
                ]
            )
            ambiguous_path = root / "ambiguous.json"
            ambiguous_path.write_text(json.dumps(ambiguous), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "choose command or evidence_validator"):
                subject.load_registry(ambiguous_path)

            directionless = registry([gate("claim", "planned")])
            del directionless["facets"][0]["gates"][0]["next_action"]
            directionless_path = root / "directionless.json"
            directionless_path.write_text(json.dumps(directionless), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "concrete next_action"):
                subject.load_registry(directionless_path)

    def test_zero_command_run_is_a_terminal_failure_not_a_vacuous_pass(self):
        facets = registry([gate("external", "external_pending")])["facets"]
        source = {
            "head": "c" * 40,
            "source_sha256": "a" * 64,
            "status_sha256": "b" * 64,
            "dirty": True,
            "file_count": 1,
            "schema_version": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry_path = self.write_registry(root, registry([gate("external", "external_pending")]))
            artifact = root / "artifact"
            with (
                mock.patch.object(subject, "source_fingerprint", return_value=source),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                code = subject.run_commands(facets, artifact, registry_path)
            self.assertEqual(code, 1)
            envelope = json.loads((artifact / "source.json").read_text(encoding="utf-8"))
            self.assertEqual(envelope["commands_run"], 0)
            self.assertEqual(envelope["run_status"], "FAIL")
            self.assertEqual(envelope["evidence_scope"], "attached_commands_only_not_facet_5x5")

    def test_reused_directory_replaces_ledger_and_emits_bound_pass_envelope(self):
        configured = registry([gate("contract", "proven", command="true")])
        facets = configured["facets"]
        source = {
            "head": "c" * 40,
            "source_sha256": "a" * 64,
            "status_sha256": "b" * 64,
            "dirty": True,
            "file_count": 1,
            "schema_version": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry_path = self.write_registry(root, configured)
            artifact = root / "artifact"
            artifact.mkdir()
            (artifact / "ledger.jsonl").write_text("stale\nstale\n", encoding="utf-8")
            with (
                mock.patch.object(subject, "source_fingerprint", return_value=source),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                code = subject.run_commands(facets, artifact, registry_path)
            self.assertEqual(code, 0)
            ledger_lines = (artifact / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(ledger_lines), 1)
            self.assertNotIn("stale", ledger_lines[0])
            envelope = json.loads((artifact / "source.json").read_text(encoding="utf-8"))
            self.assertEqual(envelope["run_status"], "PASS")
            self.assertEqual(envelope["commands_run"], 1)
            self.assertEqual(envelope["command_failures"], 0)
            self.assertTrue(envelope["source_stable"])
            self.assertFalse((artifact / "source.partial.json").exists())


if __name__ == "__main__":
    unittest.main()
