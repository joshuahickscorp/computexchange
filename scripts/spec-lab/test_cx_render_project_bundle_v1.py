#!/usr/bin/env python3
"""Adversarial tests for the additive render-project bundle preflight."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
TOOL = HERE / "cx_render_project_bundle_v1.py"
sys.path.insert(0, str(HERE))

import cx_render_project_bundle_v1 as bundle  # noqa: E402


BLEND_HEADER = b"BLENDER-v400" + b"test-scene-bytes"


class ProjectBundleFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.scene = self.root / "shots" / "hero.blend"
        self.scene.parent.mkdir()
        self.scene.write_bytes(BLEND_HEADER)
        (self.root / "textures").mkdir()
        (self.root / "textures" / "albedo.PNG").write_bytes(b"png bytes")
        (self.root / "sim-cache").mkdir()
        (self.root / "sim-cache" / "opaque.vdb").write_bytes(b"vdb bytes")
        (self.root / "unknown.asset-type").write_bytes(b"opaque bytes")

    def manifest(self) -> dict[str, object]:
        return bundle.build_manifest(self.root, "shots/hero.blend")

    def test_manifest_is_deterministic_and_binds_every_opaque_regular_file(self) -> None:
        first = self.manifest()
        second = self.manifest()
        self.assertEqual(first, second)
        self.assertEqual(first["file_count"], 4)
        self.assertEqual(first["directory_count"], 3)
        self.assertEqual(first["directories"], ["shots", "sim-cache", "textures"])
        self.assertEqual(
            [entry["path"] for entry in first["entries"]],
            [
                "shots/hero.blend",
                "sim-cache/opaque.vdb",
                "textures/albedo.PNG",
                "unknown.asset-type",
            ],
        )
        self.assertEqual(
            first["scene_sha256"], hashlib.sha256(BLEND_HEADER).hexdigest()
        )
        parsed = bundle.parse_manifest(bundle._canonical_json(first))
        self.assertEqual(parsed, first)
        self.assertEqual(bundle.verify_manifest(self.root, first), first)

    def test_any_byte_file_set_or_manifest_change_fails_verification(self) -> None:
        manifest = self.manifest()
        texture = self.root / "textures" / "albedo.PNG"
        texture.write_bytes(b"changed")
        with self.assertRaisesRegex(bundle.ProjectBundleError, "do not match"):
            bundle.verify_manifest(self.root, manifest)

        texture.write_bytes(b"png bytes")
        manifest = self.manifest()
        (self.root / "new.file").write_bytes(b"added")
        with self.assertRaisesRegex(bundle.ProjectBundleError, "do not match"):
            bundle.verify_manifest(self.root, manifest)

        (self.root / "new.file").unlink()
        manifest = self.manifest()
        tampered = json.loads(json.dumps(manifest))
        tampered["entries"][0]["bytes"] += 1
        with self.assertRaisesRegex(bundle.ProjectBundleError, "total_bytes|bundle SHA"):
            bundle.parse_manifest(bundle._canonical_json(tampered))

    def test_symlinks_hardlinks_and_special_files_fail_closed(self) -> None:
        outside = self.base / "outside.bin"
        outside.write_bytes(b"outside")
        link = self.root / "link.bin"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaisesRegex(bundle.ProjectBundleError, "symlink"):
            self.manifest()
        link.unlink()

        hardlink = self.root / "hardlink.bin"
        try:
            os.link(self.root / "unknown.asset-type", hardlink)
        except OSError as exc:
            self.skipTest(f"hard links unavailable: {exc}")
        with self.assertRaisesRegex(bundle.ProjectBundleError, "hard-linked"):
            self.manifest()
        hardlink.unlink()

        if hasattr(os, "mkfifo"):
            fifo = self.root / "pipe"
            os.mkfifo(fifo)
            self.addCleanup(lambda: fifo.exists() and fifo.unlink())
            with self.assertRaisesRegex(bundle.ProjectBundleError, "non-regular"):
                self.manifest()

    def test_cross_platform_aliases_and_ambiguous_paths_are_rejected(self) -> None:
        for path in (
            "../hero.blend",
            "./hero.blend",
            "/hero.blend",
            "a//hero.blend",
            "a\\hero.blend",
            "a/hero.BLEND",
            "CON/hero.blend",
            "bad./hero.blend",
            "bad\x00/hero.blend",
        ):
            with self.subTest(path=path):
                with self.assertRaises(bundle.ProjectBundleError):
                    bundle.strict_relative_path(path, require_blend=True)

        upper = self.root / "Alias.bin"
        lower = self.root / "alias.bin"
        upper.write_bytes(b"one")
        try:
            lower.write_bytes(b"two")
        except OSError as exc:
            self.skipTest(f"case-distinct names unavailable: {exc}")
        if upper.samefile(lower):
            # The host cannot materialize both aliases, so exercise the same
            # fail-closed boundary at manifest ingress with two canonical rows.
            manifest = self.manifest()
            source = next(
                entry for entry in manifest["entries"] if entry["path"] == "Alias.bin"
            )
            alias = dict(source)
            alias["path"] = "alias.bin"
            manifest["entries"].append(alias)
            manifest["entries"].sort(key=lambda entry: entry["path"].encode("utf-8"))
            manifest["file_count"] += 1
            manifest["total_bytes"] += alias["bytes"]
            manifest["bundle_sha256"] = bundle._bundle_sha256(
                manifest["entries"], manifest["directories"]
            )
            manifest["manifest_sha256"] = bundle._manifest_sha256(manifest)
            with self.assertRaisesRegex(bundle.ProjectBundleError, "alias"):
                bundle.parse_manifest(bundle._canonical_json(manifest))
        else:
            with self.assertRaisesRegex(bundle.ProjectBundleError, "case-insensitive"):
                self.manifest()

    def test_scene_must_exist_be_lowercase_blend_and_have_blender_header(self) -> None:
        with self.assertRaisesRegex(bundle.ProjectBundleError, "absent"):
            bundle.build_manifest(self.root, "missing.blend")
        with self.assertRaisesRegex(bundle.ProjectBundleError, "lowercase"):
            bundle.build_manifest(self.root, "shots/hero.BLEND")
        self.scene.write_bytes(b"not a blend")
        with self.assertRaisesRegex(bundle.ProjectBundleError, "Blender file header"):
            self.manifest()

    def test_limits_are_applied_before_a_manifest_can_grow_unbounded(self) -> None:
        with mock.patch.object(bundle, "MAX_FILES", 3):
            with self.assertRaisesRegex(bundle.ProjectBundleError, "exceeds 3 files"):
                self.manifest()
        with mock.patch.object(bundle, "MAX_TOTAL_BYTES", 1):
            with self.assertRaisesRegex(bundle.ProjectBundleError, "total bytes"):
                self.manifest()
        with mock.patch.object(bundle, "MAX_FILE_BYTES", 1):
            with self.assertRaisesRegex(bundle.ProjectBundleError, "exceeds 1 bytes"):
                self.manifest()

    def test_file_mutation_during_descriptor_hash_fails_closed(self) -> None:
        real_fstat = bundle.os.fstat
        calls = 0

        def changed_after_read(descriptor: int):
            nonlocal calls
            info = real_fstat(descriptor)
            calls += 1
            if calls != 2:
                return info
            return SimpleNamespace(
                st_dev=info.st_dev,
                st_ino=info.st_ino,
                st_mode=info.st_mode,
                st_nlink=info.st_nlink,
                st_uid=info.st_uid,
                st_gid=info.st_gid,
                st_size=info.st_size + 1,
                st_mtime_ns=info.st_mtime_ns,
                st_ctime_ns=info.st_ctime_ns,
            )

        with mock.patch.object(bundle.os, "fstat", side_effect=changed_after_read):
            with self.assertRaisesRegex(bundle.ProjectBundleError, "changed while hashed"):
                bundle._snapshot_file(
                    self.scene, "shots/hero.blend", scene=True
                )

    def test_file_rewrite_after_early_hash_fails_final_identity_replay(self) -> None:
        real_snapshot = bundle._snapshot_file
        mutated = False

        def mutate_after_scene(path: Path, relative: str, *, scene: bool):
            nonlocal mutated
            result = real_snapshot(path, relative, scene=scene)
            if scene and not mutated:
                path.write_bytes(BLEND_HEADER + b"changed-after-hash")
                mutated = True
            return result

        with mock.patch.object(bundle, "_snapshot_file", side_effect=mutate_after_scene):
            with self.assertRaisesRegex(bundle.ProjectBundleError, "hash pass"):
                self.manifest()
        self.assertTrue(mutated)

    def test_empty_directories_are_bound_and_cross_platform_aliased(self) -> None:
        manifest = self.manifest()
        empty = self.root / "empty"
        empty.mkdir()
        changed = self.manifest()
        self.assertNotEqual(changed["bundle_sha256"], manifest["bundle_sha256"])
        with self.assertRaisesRegex(bundle.ProjectBundleError, "do not match"):
            bundle.verify_manifest(self.root, manifest)
        empty.rmdir()

        upper = self.root / "EmptyDir"
        lower = self.root / "emptydir"
        upper.mkdir()
        try:
            lower.mkdir()
        except FileExistsError:
            # Case-insensitive hosts cannot materialize both names, so prove the
            # portable alias check at the strict manifest boundary instead.
            manifest = self.manifest()
            manifest["directories"].append("emptydir")
            manifest["directories"].sort(key=lambda path: path.encode("utf-8"))
            manifest["directory_count"] += 1
            manifest["bundle_sha256"] = bundle._bundle_sha256(
                manifest["entries"], manifest["directories"]
            )
            manifest["manifest_sha256"] = bundle._manifest_sha256(manifest)
            with self.assertRaisesRegex(bundle.ProjectBundleError, "alias"):
                bundle.parse_manifest(bundle._canonical_json(manifest))
        else:
            with self.assertRaisesRegex(bundle.ProjectBundleError, "case-insensitive"):
                self.manifest()

    def test_parser_rejects_unknown_duplicate_noncanonical_and_wrong_scalar_types(self) -> None:
        manifest = self.manifest()
        extra = dict(manifest)
        extra["surprise"] = True
        with self.assertRaisesRegex(bundle.ProjectBundleError, "top-level"):
            bundle.parse_manifest(bundle._canonical_json(extra))

        duplicate = bundle._canonical_json(manifest).replace(
            b'{"bundle_sha256":', b'{"kind":"duplicate","bundle_sha256":', 1
        )
        with self.assertRaisesRegex(bundle.ProjectBundleError, "duplicate"):
            bundle.parse_manifest(duplicate)

        wrong = dict(manifest)
        wrong["file_count"] = True
        with self.assertRaisesRegex(bundle.ProjectBundleError, "file_count"):
            bundle.parse_manifest(bundle._canonical_json(wrong))

        wrong = dict(manifest)
        wrong["schema_version"] = True
        with self.assertRaisesRegex(bundle.ProjectBundleError, "schema_version"):
            bundle.parse_manifest(bundle._canonical_json(wrong))

        reordered = json.loads(json.dumps(manifest))
        reordered["entries"] = list(reversed(reordered["entries"]))
        reordered["manifest_sha256"] = bundle._manifest_sha256(reordered)
        with self.assertRaisesRegex(bundle.ProjectBundleError, "canonical path order"):
            bundle.parse_manifest(bundle._canonical_json(reordered))

    def test_cli_publishes_new_manifest_outside_root_and_verifies_it(self) -> None:
        output = self.base / "project-manifest.json"
        created = subprocess.run(
            [
                sys.executable,
                str(TOOL),
                "create",
                "--root",
                str(self.root),
                "--scene",
                "shots/hero.blend",
                "--output",
                str(output),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(created.returncode, 0, created.stderr.decode())
        self.assertTrue(output.is_file())
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

        verified = subprocess.run(
            [
                sys.executable,
                str(TOOL),
                "verify",
                "--root",
                str(self.root),
                "--manifest",
                str(output),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(verified.returncode, 0, verified.stderr.decode())
        proof = json.loads(verified.stdout)
        self.assertTrue(proof["ok"])
        self.assertEqual(proof["file_count"], 4)

        replaced = subprocess.run(
            [
                sys.executable,
                str(TOOL),
                "create",
                "--root",
                str(self.root),
                "--scene",
                "shots/hero.blend",
                "--output",
                str(output),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(replaced.returncode, 2)
        self.assertIn(b"refusing to replace", replaced.stderr)

        inside = subprocess.run(
            [
                sys.executable,
                str(TOOL),
                "create",
                "--root",
                str(self.root),
                "--scene",
                "shots/hero.blend",
                "--output",
                str(self.root / "manifest.json"),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(inside.returncode, 2)
        self.assertIn(b"outside", inside.stderr)

        missing_parent = subprocess.run(
            [
                sys.executable,
                str(TOOL),
                "create",
                "--root",
                str(self.root),
                "--scene",
                "shots/hero.blend",
                "--output",
                str(self.base / "missing" / "manifest.json"),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(missing_parent.returncode, 2)
        self.assertIn(b"parent is unavailable", missing_parent.stderr)
        self.assertNotIn(b"Traceback", missing_parent.stderr)


if __name__ == "__main__":
    unittest.main()
