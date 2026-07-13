#!/usr/bin/env python3
"""Local-only contract and safety tests for scripts/fleet_proof.py."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import fleet_proof  # noqa: E402


FIXTURE = ROOT / "docs" / "fleet-proof" / "mock.inventory.json"


class _FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FleetProofTest(unittest.TestCase):
    def setUp(self):
        self.inventory = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.tmp = tempfile.TemporaryDirectory()
        self.artifacts = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_mock(self, inventory=None, run_id="test-run"):
        return fleet_proof.run_proof(
            inventory or self.inventory,
            "mock",
            self.artifacts,
            run_id,
        )

    def test_four_node_mock_produces_pass_ledger_summary_and_hash_manifest(self):
        rc, root, summary = self.run_mock()
        self.assertEqual(rc, 0)
        self.assertEqual(summary["status"], "PASS")
        self.assertEqual(len(summary["readiness"]), 4)
        self.assertEqual(len(summary["receipts"]), 4)
        self.assertTrue(summary["assertions"])
        self.assertTrue(all(row["passed"] for row in summary["assertions"]))

        events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
        self.assertEqual(events[-1]["event"], "run_finished")
        self.assertEqual(events[-1]["status"], "pass")
        self.assertEqual(sum(row["event"] == "node_ready" and row["status"] == "pass" for row in events), 4)
        self.assertEqual(sum(row["event"] == "assertion" for row in events), len(summary["assertions"]))

        manifest = json.loads((root / "artifact_manifest.json").read_text())
        manifest_by_path = {row["path"]: row for row in manifest["files"]}
        self.assertIn("summary.json", manifest_by_path)
        self.assertEqual(
            manifest_by_path["summary.json"]["sha256"],
            fleet_proof.sha256_file(root / "summary.json"),
        )

    def test_dry_run_executes_no_commands_and_records_plan(self):
        rc, root, summary = fleet_proof.run_proof(
            self.inventory,
            "dry-run",
            self.artifacts,
            "dry-plan",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(summary["status"], "PLANNED")
        self.assertFalse((root / "commands").exists())
        events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
        self.assertEqual(sum(row["event"] == "node_planned" for row in events), 4)

    def test_duplicate_physical_machine_fails_and_still_cleans_every_scope(self):
        inventory = copy.deepcopy(self.inventory)
        inventory["nodes"][1]["mock"]["ready"]["machine_id"] = inventory["nodes"][0]["mock"]["ready"]["machine_id"]
        rc, _root, summary = self.run_mock(inventory, "duplicate-machine")
        self.assertEqual(rc, 1)
        self.assertEqual(summary["status"], "FAIL")
        self.assertIn("ready.min_distinct_machines", summary["error"])
        # Workflow was never submitted, so cleanup covers four nodes + four providers.
        self.assertEqual(len(summary["cleanup"]), 8)
        self.assertTrue(all(row.get("status") != "manual-action-required" for row in summary["cleanup"]))

    def test_duplicate_worker_id_is_never_counted_as_two_agents(self):
        inventory = copy.deepcopy(self.inventory)
        inventory["nodes"][1]["mock"]["ready"]["worker_id"] = inventory["nodes"][0]["mock"]["ready"]["worker_id"]
        rc, _root, summary = self.run_mock(inventory, "duplicate-worker")
        self.assertEqual(rc, 1)
        self.assertIn("ready.worker_ids_unique", summary["error"])

    def test_wrong_verification_class_fails_receipt_proof(self):
        inventory = copy.deepcopy(self.inventory)
        inventory["workflow"]["mock"]["collect"]["receipts"][0]["verification_class"] = "candle|wrong-build"
        rc, _root, summary = self.run_mock(inventory, "wrong-class")
        self.assertEqual(rc, 1)
        self.assertIn("receipt[0].verification_class", summary["error"])

    def test_unknown_receipt_worker_cannot_satisfy_lane_cardinality(self):
        inventory = copy.deepcopy(self.inventory)
        inventory["workflow"]["mock"]["collect"]["receipts"][0]["worker_id"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        rc, _root, summary = self.run_mock(inventory, "unknown-worker")
        self.assertEqual(rc, 1)
        self.assertIn("receipt[0].known_worker", summary["error"])

    def test_cleanup_failure_changes_an_otherwise_passing_run_to_fail(self):
        inventory = copy.deepcopy(self.inventory)
        inventory["nodes"][0]["mock"]["cleanup_rc"] = 9
        rc, _root, summary = self.run_mock(inventory, "cleanup-fail")
        self.assertEqual(rc, 1)
        self.assertEqual(summary["status"], "FAIL")
        self.assertIn("cleanup requires manual action", summary["error"])
        self.assertTrue(any(row.get("status") == "manual-action-required" for row in summary["cleanup"]))

    def test_static_validation_rejects_unsatisfiable_lane_and_duplicate_resource(self):
        bad_lane = copy.deepcopy(self.inventory)
        bad_lane["requirements"]["lanes"]["cuda_vllm"] = 3
        with self.assertRaisesRegex(fleet_proof.ConfigError, "below required 3"):
            fleet_proof.validate_inventory(bad_lane, "mock")

        duplicate = copy.deepcopy(self.inventory)
        duplicate["nodes"][1]["provider"]["resource_id"] = duplicate["nodes"][0]["provider"]["resource_id"]
        with self.assertRaisesRegex(fleet_proof.ConfigError, "resource_id must be unique"):
            fleet_proof.validate_inventory(duplicate, "mock")

    def test_live_mode_refuses_ephemeral_pod_without_teardown_credentials(self):
        inventory = copy.deepcopy(self.inventory)
        for node in inventory["nodes"]:
            node["transport"] = {"kind": "local"}
            node["commands"] = {"ready": "exit 1"}
        inventory["workflow"]["commands"] = {"submit": "exit 1", "collect": "exit 1"}
        with mock.patch.dict(os.environ, {"RUNPOD_API_KEY": ""}):
            with self.assertRaisesRegex(fleet_proof.ConfigError, "refusing to touch ephemeral"):
                fleet_proof.validate_inventory(inventory, "live")

    def test_live_mode_refuses_unresolved_template_and_unpaired_start(self):
        inventory = copy.deepcopy(self.inventory)
        inventory["nodes"][0]["transport"] = {"kind": "local"}
        inventory["nodes"][0]["commands"] = {"ready": "REPLACE_WITH_READY_PROBE"}
        with self.assertRaisesRegex(fleet_proof.ConfigError, "unresolved placeholders"):
            fleet_proof.validate_inventory(inventory, "live")

        inventory = copy.deepcopy(self.inventory)
        for node in inventory["nodes"]:
            node["transport"] = {"kind": "local"}
            node["commands"] = {"ready": "true"}
        inventory["nodes"][0]["commands"]["start"] = "true"
        inventory["workflow"]["commands"] = {"submit": "true", "collect": "true"}
        with mock.patch.dict(os.environ, {"RUNPOD_API_KEY": "present"}):
            with self.assertRaisesRegex(fleet_proof.ConfigError, "start command requires a cleanup"):
                fleet_proof.validate_inventory(inventory, "live")

    def test_runpod_cleanup_uses_only_explicit_terminate_mutation(self):
        provider = self.inventory["nodes"][2]["provider"]
        response = _FakeHTTPResponse({"data": {"podTerminate": True}})
        with mock.patch.dict(os.environ, {"RUNPOD_API_KEY": "rpa_test_secret"}), mock.patch.object(
            fleet_proof.urllib.request, "urlopen", return_value=response
        ) as opened:
            ok, detail = fleet_proof.terminate_runpod(provider, retries=0)
        self.assertTrue(ok)
        self.assertEqual(detail, "terminated")
        request = opened.call_args.args[0]
        body = json.loads(request.data)
        self.assertIn("podTerminate", body["query"])
        self.assertNotIn("podFindAndDeploy", body["query"])
        self.assertEqual(body["variables"]["i"], provider["resource_id"])

    def test_final_json_contract_and_secret_redaction(self):
        payload = fleet_proof.parse_final_json("log line\n{\"ok\":true,\"n\":2}\n", "test")
        self.assertEqual(payload, {"ok": True, "n": 2})
        with self.assertRaisesRegex(fleet_proof.ProofError, "final non-empty stdout line"):
            fleet_proof.parse_final_json("{\"ok\":true}\ntrailing log\n", "test")

        with mock.patch.dict(os.environ, {"RUNPOD_API_KEY": "rpa_test_secret"}):
            redacted = fleet_proof.redact_text(
                "Authorization: Bearer rpa_test_secret api_key=rpa_test_secret"
            )
        self.assertNotIn("rpa_test_secret", redacted)


if __name__ == "__main__":
    unittest.main()
