#!/usr/bin/env python3
"""Adversarial tests for the terminal prove-local evidence validator."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import verify_proof_ledger as subject  # noqa: E402


SOURCE = "a" * 64
STATUS = "b" * 64


def rows() -> list[str]:
    return [
        "META\tcommit\t" + "c" * 40,
        "META\tdirty\ttrue",
        "META\tsource_sha256\t" + SOURCE,
        "META\tstatus_sha256\t" + STATUS,
        "META\tstarted_at\t2026-07-10T00:00:00Z",
        "META\tproof_mode\tcontract_only",
        "PASS\tmatrix:TestAtomicThing\tdeterministic check",
        "META\tsource_sha256_end\t" + SOURCE,
        "META\tstatus_sha256_end\t" + STATUS,
        "PASS\tsource-stability\tstable",
        "META\tcompleted_at\t2026-07-10T00:01:00Z",
        "META\tstatus\tPASS",
    ]


class VerifyProofLedgerTest(unittest.TestCase):
    def write(self, root: Path, values: list[str]) -> Path:
        path = root / "ledger.txt"
        path.write_text("\n".join(values) + "\n", encoding="utf-8")
        return path

    def test_accepts_terminal_source_bound_required_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(Path(tmp), rows())
            with mock.patch.object(
                subject,
                "source_fingerprint",
                return_value={"source_sha256": SOURCE, "status_sha256": STATUS},
            ):
                result = subject.validate_ledger(
                    path,
                    required_mode="contract_only",
                    required_passes=["matrix:TestAtomicThing"],
                    require_current_source=True,
                )
            self.assertEqual(result["meta"]["status"], "PASS")

    def test_rejects_partial_stale_failed_and_wrong_mode_ledgers(self):
        cases: list[tuple[str, list[str], dict[str, object]]] = []
        partial = rows()[:-2]
        cases.append(("missing terminal", partial, {}))
        changed = rows()
        changed[7] = "META\tsource_sha256_end\t" + "d" * 64
        cases.append(("fingerprints differ", changed, {}))
        failed = rows()
        failed.insert(7, "FAIL\tmatrix:TestAtomicThing\tboom")
        cases.append(("contains FAIL", failed, {}))
        cases.append(("required mode", rows(), {"required_mode": "full_local"}))
        cases.append(("required PASS", rows(), {"required_passes": ["missing"]}))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index, (message, values, kwargs) in enumerate(cases):
                with self.subTest(message=message):
                    case_root = root / str(index)
                    case_root.mkdir()
                    path = self.write(case_root, values)
                    with self.assertRaises(subject.LedgerError):
                        subject.validate_ledger(path, **kwargs)

    def test_rejects_current_source_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(Path(tmp), rows())
            with mock.patch.object(
                subject,
                "source_fingerprint",
                return_value={"source_sha256": "e" * 64, "status_sha256": STATUS},
            ):
                with self.assertRaisesRegex(subject.LedgerError, "stale for current source"):
                    subject.validate_ledger(path, require_current_source=True)


if __name__ == "__main__":
    unittest.main()
