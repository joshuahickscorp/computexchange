#!/usr/bin/env python3
"""Tests for the dirty-worktree source fingerprint used by proof ledgers."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import source_fingerprint as subject  # noqa: E402


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class SourceFingerprintTest(unittest.TestCase):
    def test_fingerprint_tracks_exact_source_but_ignores_ignored_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git(root, "init")
            git(root, "config", "user.email", "proof@example.invalid")
            git(root, "config", "user.name", "Proof Test")
            (root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
            (root / "tracked.txt").write_text("tracked-v1\n", encoding="utf-8")
            git(root, "add", ".gitignore", "tracked.txt")
            git(root, "commit", "-m", "fixture")

            clean = subject.source_fingerprint(root)
            self.assertFalse(clean["dirty"])
            self.assertEqual(clean, subject.source_fingerprint(root))

            ignored = root / "ignored" / "build.bin"
            ignored.parent.mkdir()
            ignored.write_bytes(b"ignored-v1")
            self.assertEqual(clean, subject.source_fingerprint(root))
            ignored.write_bytes(b"ignored-v2")
            self.assertEqual(clean, subject.source_fingerprint(root))

            (root / "tracked.txt").write_text("tracked-v2\n", encoding="utf-8")
            tracked_change = subject.source_fingerprint(root)
            self.assertTrue(tracked_change["dirty"])
            self.assertNotEqual(clean["source_sha256"], tracked_change["source_sha256"])

            (root / "tracked.txt").write_text("tracked-v1\n", encoding="utf-8")
            (root / "untracked.txt").write_text("untracked-v1\n", encoding="utf-8")
            untracked_v1 = subject.source_fingerprint(root)
            (root / "untracked.txt").write_text("untracked-v2\n", encoding="utf-8")
            untracked_v2 = subject.source_fingerprint(root)
            self.assertTrue(untracked_v1["dirty"])
            self.assertNotEqual(untracked_v1["source_sha256"], untracked_v2["source_sha256"])

    def test_executable_bit_is_part_of_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git(root, "init")
            git(root, "config", "user.email", "proof@example.invalid")
            git(root, "config", "user.name", "Proof Test")
            script = root / "tool.sh"
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            script.chmod(0o644)
            git(root, "add", "tool.sh")
            git(root, "commit", "-m", "fixture")
            before = subject.source_fingerprint(root)
            script.chmod(0o755)
            after = subject.source_fingerprint(root)
            self.assertNotEqual(before["source_sha256"], after["source_sha256"])


if __name__ == "__main__":
    unittest.main()
