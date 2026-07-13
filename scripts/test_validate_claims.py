#!/usr/bin/env python3
"""Adversarial tests for outward-facing claim conformance."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_claims import ClaimPolicyError, load_policy, validate_claims


class ClaimPolicyTest(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[Path, dict[str, object]]:
        (root / "active.md").write_text(
            "central boundary\nno real payout\n", encoding="utf-8"
        )
        (root / "history.md").write_text(
            "CLAIM-SCOPE: archive\nnot current\n", encoding="utf-8"
        )
        (root / "internal.md").write_text(
            "CLAIM-SCOPE: internal\nworking notes\n", encoding="utf-8"
        )
        policy: dict[str, object] = {
            "schema_version": 1,
            "surface_discovery": {
                "patterns": ["*.md"],
                "ignored_prefixes": ["ignored/"],
            },
            "active_surfaces": ["active.md"],
            "archival_surfaces": [
                {
                    "path": "history.md",
                    "required_marker": "CLAIM-SCOPE: archive",
                    "required_fragments": ["not current"],
                }
            ],
            "internal_surfaces": [
                {
                    "path": "internal.md",
                    "required_marker": "CLAIM-SCOPE: internal",
                }
            ],
            "forbidden_active_patterns": [
                {
                    "id": "universal",
                    "gate": "runtime/claim",
                    "pattern": "(?i)all runtimes",
                    "reason": "unsupported",
                }
            ],
            "required_active_fragments": [
                {
                    "path": "active.md",
                    "gate": "money/real",
                    "fragment": "no real payout",
                }
            ],
        }
        path = root / "policy.json"
        path.write_text(json.dumps(policy), encoding="utf-8")
        return path, policy

    def test_valid_policy_and_boundaries_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, _ = self.fixture(root)
            result = validate_claims(load_policy(path), root)
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["active_surfaces"], 1)
            self.assertEqual(result["discovered_surfaces"], 3)

    def test_forbidden_claim_reports_file_line_rule_and_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, _ = self.fixture(root)
            (root / "active.md").write_text(
                "central boundary\nall runtimes supported\nno real payout\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ClaimPolicyError, r"active\.md:2: universal \(runtime/claim\)"
            ):
                validate_claims(load_policy(path), root)

    def test_missing_required_boundary_and_archive_marker_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, _ = self.fixture(root)
            (root / "active.md").write_text("central boundary\n", encoding="utf-8")
            (root / "history.md").write_text("old claim\n", encoding="utf-8")
            (root / "internal.md").write_text("working notes\n", encoding="utf-8")
            with self.assertRaises(ClaimPolicyError) as raised:
                validate_claims(load_policy(path), root)
            message = str(raised.exception)
            self.assertIn("missing required boundary", message)
            self.assertIn("missing archival marker", message)
            self.assertIn("missing archival warning", message)
            self.assertIn("missing internal marker", message)

    def test_new_outward_file_cannot_escape_surface_classification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, _ = self.fixture(root)
            (root / "new-product-page.md").write_text(
                "all runtimes supported\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                ClaimPolicyError,
                r"unclassified outward surfaces: new-product-page\.md",
            ):
                validate_claims(load_policy(path), root)

    def test_malformed_policy_rejects_duplicate_rules_and_bad_regex(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, policy = self.fixture(root)
            rule = policy["forbidden_active_patterns"][0]
            policy["forbidden_active_patterns"] = [rule, dict(rule)]
            path.write_text(json.dumps(policy), encoding="utf-8")
            with self.assertRaisesRegex(ClaimPolicyError, "duplicate forbidden"):
                load_policy(path)

            policy["forbidden_active_patterns"] = [dict(rule, id="bad", pattern="[")]
            path.write_text(json.dumps(policy), encoding="utf-8")
            with self.assertRaisesRegex(ClaimPolicyError, "invalid regex"):
                load_policy(path)

    def test_static_proof_counter_rule_rejects_counts_not_scores_or_http_codes(self):
        canonical = json.loads(
            (Path(__file__).resolve().parents[1] / "proof" / "claims" / "claim-policy.json").read_text(
                encoding="utf-8"
            )
        )
        counter_rule = next(
            rule
            for rule in canonical["forbidden_active_patterns"]
            if rule["id"] == "stale_proof_count"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "active.md").write_text(
                "5/5 is a gate score; 401/403 are HTTP statuses; 184 / 184 pass is a stale counter.\n",
                encoding="utf-8",
            )
            policy = {
                "schema_version": 1,
                "surface_discovery": {
                    "patterns": ["active.md"],
                    "ignored_prefixes": ["ignored/"],
                },
                "active_surfaces": ["active.md"],
                "archival_surfaces": [],
                "internal_surfaces": [],
                "forbidden_active_patterns": [counter_rule],
                "required_active_fragments": [],
            }
            path = root / "policy.json"
            path.write_text(json.dumps(policy), encoding="utf-8")
            with self.assertRaises(ClaimPolicyError) as raised:
                validate_claims(load_policy(path), root)
            message = str(raised.exception)
            self.assertIn("184 / 184 pass", message)
            self.assertNotIn("'5/5'", message)
            self.assertNotIn("'401/403'", message)


if __name__ == "__main__":
    unittest.main()
