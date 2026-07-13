#!/usr/bin/env python3
"""Fail-closed tests for the agent-side generalized render preview driver."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cx_agent_render_preview_driver as driver  # noqa: E402


GOOD_BACKEND = r'''
from cx_speculative_core import DraftProposal, RepairResult, Verification

PROTOCOL_VERSION = 1
MODALITY = "render"

def baseline(unit):
    return unit.payload["truth"]

def draft(unit):
    return DraftProposal(unit, unit.payload["draft"])

def verify(proposal):
    truth = proposal.unit.payload["truth"]
    return Verification(proposal.draft == truth, truth)

def repair(_proposal, verification):
    return RepairResult(verification.truth)
'''


BAD_BACKEND = r'''
PROTOCOL_VERSION = 1
MODALITY = "render"

def baseline(unit):
    return unit.payload["truth"]

def draft(_unit):
    raise RuntimeError("draft exploded")

def verify(_proposal):
    raise AssertionError("unreachable")

def repair(_proposal, _verification):
    raise AssertionError("unreachable")
'''


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def request(*units: dict) -> bytes:
    return json.dumps({
        "schema_version": 1,
        "kind": driver.REQUEST_KIND,
        "units": [
            {"unit_id": f"tile-{i}", "payload": unit, "meta": {}}
            for i, unit in enumerate(units)
        ],
        "meta": {"job": "unit-test"},
    }).encode()


class RenderPreviewDriverTest(unittest.TestCase):
    def env_for(self, backend: Path) -> dict[str, str]:
        return {
            driver.BACKEND_ENV: str(backend.resolve()),
            driver.BACKEND_SHA_ENV: sha(backend),
            driver.CORE_SHA_ENV: sha(driver.CORE_PATH),
            driver.ADAPTER_SHA_ENV: sha(driver.ADAPTER_PATH),
        }

    def test_real_generic_controller_accepts_and_repairs_without_baseline_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = Path(tmp) / "backend.py"
            backend.write_text(GOOD_BACKEND)
            with mock.patch.dict(os.environ, self.env_for(backend), clear=False):
                out = driver.execute_request(request(
                    {"draft": "A", "truth": "A"},
                    {"draft": "wrong", "truth": "B"},
                ))

        self.assertEqual(out["outputs"], ["A", "B"])
        self.assertTrue(out["preview_only"])
        self.assertFalse(out["billing_eligible"])
        self.assertFalse(out["production_ready"])
        receipt = out["receipt"]
        self.assertEqual(receipt["modality"], "render")
        self.assertEqual(receipt["branch_id"], driver.BRANCH_ID)
        self.assertEqual(receipt["quality_tier"], "preview")
        self.assertEqual(receipt["accepted_units"], 1)
        self.assertEqual(receipt["repaired_units"], 1)
        self.assertEqual(receipt["baseline_source"], "absent")
        self.assertEqual(receipt["baseline_total_time_s"], 0.0)
        self.assertIsNone(receipt["speedup_vs_baseline"])
        self.assertFalse(receipt["artifact_verified"])
        self.assertEqual(receipt["evidence"], "synthetic")
        self.assertFalse(receipt["exact"])  # render fidelity is never token exactness

    def test_backend_failure_falls_back_and_batch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = Path(tmp) / "backend.py"
            backend.write_text(BAD_BACKEND)
            with mock.patch.dict(os.environ, self.env_for(backend), clear=False):
                out = driver.execute_request(request(
                    {"draft": "wrong", "truth": "reference"},
                ))

        self.assertEqual(out["outputs"], ["reference"])
        self.assertEqual(out["receipt"]["quality_tier"], "fail")
        self.assertFalse(out["receipt"]["quality_gate"])
        self.assertFalse(out["receipt"]["artifact_verified"])

    def test_backend_and_controller_hashes_are_required_and_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = Path(tmp) / "backend.py"
            backend.write_text(GOOD_BACKEND)
            env = self.env_for(backend)
            env[driver.BACKEND_SHA_ENV] = "0" * 64
            with mock.patch.dict(os.environ, env, clear=False):
                with self.assertRaisesRegex(driver.PreviewProtocolError, "mismatch"):
                    driver.execute_request(request({"draft": 1, "truth": 1}))

            env = self.env_for(backend)
            env[driver.CORE_SHA_ENV] = "f" * 64
            with mock.patch.dict(os.environ, env, clear=False):
                with self.assertRaisesRegex(driver.PreviewProtocolError, "mismatch"):
                    driver.execute_request(request({"draft": 1, "truth": 1}))

    def test_unpinned_controller_bytes_never_execute(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            backend = tmp / "backend.py"
            backend.write_text(GOOD_BACKEND)
            marker = tmp / "controller-executed"
            malicious_core = tmp / "cx_speculative_core.py"
            malicious_core.write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('executed')\n"
            )
            env = self.env_for(backend)
            # Deliberately do NOT authorize the malicious source.
            env[driver.CORE_SHA_ENV] = "0" * 64
            with (
                mock.patch.dict(os.environ, env, clear=False),
                mock.patch.object(driver, "CORE_PATH", malicious_core),
            ):
                with self.assertRaisesRegex(driver.PreviewProtocolError, "mismatch"):
                    driver.execute_request(request({"draft": 1, "truth": 1}))
            self.assertFalse(marker.exists(), "hash-mismatched controller code executed")

    def test_request_cannot_select_code_and_rejects_duplicates_or_unknown_fields(self):
        body = json.loads(request({"draft": 1, "truth": 1}))
        body["backend"] = "/tmp/buyer.py"
        with self.assertRaisesRegex(driver.PreviewProtocolError, "unknown"):
            driver.parse_request(json.dumps(body).encode())

        duplicate = (
            '{"schema_version":1,"schema_version":1,'
            '"kind":"cx_spec_render_preview_request","units":[]}'
        ).encode()
        with self.assertRaisesRegex(driver.PreviewProtocolError, "duplicate"):
            driver.parse_request(duplicate)

    def test_request_bounds_units_ids_and_nesting(self):
        with self.assertRaisesRegex(driver.PreviewProtocolError, "1.."):
            driver.parse_request(json.dumps({
                "schema_version": 1,
                "kind": driver.REQUEST_KIND,
                "units": [],
            }).encode())

        duplicate_ids = json.dumps({
            "schema_version": 1,
            "kind": driver.REQUEST_KIND,
            "units": [
                {"unit_id": "same", "payload": 1, "meta": {}},
                {"unit_id": "same", "payload": 2, "meta": {}},
            ],
        }).encode()
        with self.assertRaisesRegex(driver.PreviewProtocolError, "duplicate unit_id"):
            driver.parse_request(duplicate_ids)

        bad_id = json.dumps({
            "schema_version": 1,
            "kind": driver.REQUEST_KIND,
            "units": [{"unit_id": [], "payload": 1, "meta": {}}],
        }).encode()
        with self.assertRaisesRegex(driver.PreviewProtocolError, "unit_id"):
            driver.parse_request(bad_id)

        bad_meta = json.dumps({
            "schema_version": 1,
            "kind": driver.REQUEST_KIND,
            "units": [{"unit_id": "tile", "payload": 1, "meta": []}],
        }).encode()
        with self.assertRaisesRegex(driver.PreviewProtocolError, "meta must be an object"):
            driver.parse_request(bad_meta)


if __name__ == "__main__":
    unittest.main()
