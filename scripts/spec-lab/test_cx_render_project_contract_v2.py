#!/usr/bin/env python3
"""Adversarial tests for the wire-only v2 full-project preflight contract."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
TOOL = HERE / "cx_render_project_contract_v2.py"
sys.path.insert(0, str(HERE))

import cx_render_project_bundle_v1 as bundle_v1  # noqa: E402
import cx_render_project_contract_v2 as contract_v2  # noqa: E402


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    return contract_v2.canonical_json(value)


class ProjectContractFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "project"
        self.root.mkdir()
        (self.root / "scene.blend").write_bytes(b"BLENDER-v400project scene")
        (self.root / "textures").mkdir()
        (self.root / "textures" / "hero.exr").write_bytes(b"opaque texture")
        self.object = self.base / "project.tar.zst"
        self.object.write_bytes(b"sealed project-object fixture")
        self.manifest = bundle_v1.build_manifest(self.root, "scene.blend")
        self.manifest_raw = bundle_v1._canonical_json(self.manifest) + b"\n"
        self.request = self.valid_request()
        self.request_raw = canonical(self.request) + b"\n"

    def valid_request(self) -> dict[str, object]:
        sha = lambda character: character * 64
        return {
            "schema_version": 2,
            "kind": contract_v2.REQUEST_KIND,
            "project_object": {
                "object_key": "projects/demo/project.tar.zst",
                "object_version": "v1.ABC-123_~+=:@",
                "sha256": digest(self.object.read_bytes()),
                "bytes": len(self.object.read_bytes()),
            },
            "frame_range": {"start": 1, "end": 3, "step": 1, "count": 3},
            "render": {
                "feature_cell": "cycles_cpu_png_rgba8_combined_no_post_v1",
                "engine": "CYCLES",
                "device": "CPU",
                "width": 64,
                "height": 64,
                "camera": "Camera",
                "view_layer": "ViewLayer",
                "reference_samples": 64,
                "draft_samples": 4,
                "verify_samples": 4,
                "repair_samples": 32,
                "seed_policy_sha256": sha("1"),
                "use_motion_blur": False,
                "use_denoising": False,
            },
            "output": {
                "file_format": "PNG",
                "color_mode": "RGBA",
                "color_depth": "8",
                "codec": "DEFLATE_0",
                "passes": ["Combined"],
                "views": ["MAIN"],
                "use_compositor": False,
                "use_sequencer": False,
                "use_freestyle": False,
                "use_multiview": False,
                "transparent": False,
                "color_management_sha256": sha("2"),
                "output_policy_sha256": sha("3"),
            },
            "policies": {
                "render_policy_sha256": sha("4"),
                "speculation_policy_sha256": sha("5"),
                "fallback_policy_sha256": sha("6"),
                "cache_policy_sha256": sha("7"),
                "fallback_required": True,
                "fail_closed": True,
                "requested_quality_tier": "delivery",
            },
            "runtime": {
                "blender_sha256": sha("8"),
                "runtime_image_sha256": sha("9"),
                "agent_sha256": sha("a"),
                "executor_sha256": sha("b"),
                "dependency_scanner_sha256": sha("c"),
                "sandbox_policy_sha256": sha("d"),
            },
            "verifier": {
                "verifier_sha256": sha("e"),
                "verifier_policy_sha256": sha("f"),
                "selection_policy_sha256": sha("0"),
                "independent_reference_required": True,
                "server_selected": True,
            },
            "resources": {
                "max_duration_secs": 3_600,
                "max_memory_bytes": 8 << 30,
                "max_disk_bytes": 128 << 20,
                "max_input_object_bytes": 1 << 20,
                "max_project_bytes": 1 << 20,
                "max_manifest_bytes": 1 << 20,
                "max_output_bytes": 64 << 20,
                "max_project_files": 16,
                "max_project_directories": 16,
                "max_frames": 16,
                "max_pixels_per_frame": 64 * 64,
                "max_total_pixel_frames": 64 * 64 * 16,
                "max_processes": 64,
                "max_output_objects": 16,
            },
        }

    def build(self) -> dict[str, object]:
        return contract_v2.build_contract(
            self.root, self.manifest_raw, self.object, self.request_raw
        )

    def verify(self, contract: dict[str, object]) -> dict[str, object]:
        return contract_v2.verify_contract(
            self.root,
            self.manifest_raw,
            self.object,
            self.request_raw,
            canonical(contract),
        )

    def test_contract_binds_every_identity_and_stays_structurally_wire_only(self) -> None:
        first = self.build()
        second = self.build()
        self.assertEqual(first, second)
        self.assertEqual(self.verify(first), first)
        self.assertEqual(first["authorization"], contract_v2.AUTHORIZATION)
        self.assertEqual(first["preflight"], contract_v2.PREFLIGHT)
        self.assertTrue(first["preflight"]["request_feature_cell_schema_validated"])
        self.assertNotIn("feature_cell_validated", first["preflight"])
        self.assertFalse(first["authorization"]["execution_enabled"])
        self.assertFalse(first["authorization"]["production_ready"])
        self.assertFalse(first["authorization"]["billing_eligible"])
        self.assertEqual(first["project"]["object_version"], "v1.ABC-123_~+=:@")
        self.assertEqual(first["project"]["manifest_sha256"], self.manifest["manifest_sha256"])
        self.assertEqual(first["project"]["bundle_sha256"], self.manifest["bundle_sha256"])
        self.assertEqual(first["project"]["scene_sha256"], self.manifest["scene_sha256"])
        self.assertEqual(
            first["project"]["directory_count"], self.manifest["directory_count"]
        )
        self.assertEqual(first["frame_range"], self.request["frame_range"])
        self.assertEqual(first["render"], self.request["render"])
        self.assertEqual(first["output"], self.request["output"])
        self.assertEqual(first["policies"], self.request["policies"])
        self.assertEqual(first["runtime"], self.request["runtime"])
        self.assertEqual(first["verifier"], self.request["verifier"])
        self.assertEqual(first["resources"], self.request["resources"])

    def test_request_rejects_duplicate_unknown_malformed_and_nonfinite_fields(self) -> None:
        unknown = json.loads(json.dumps(self.request))
        unknown["render"]["surprise"] = True
        with self.assertRaisesRegex(contract_v2.ProjectContractError, "unknown or missing"):
            contract_v2.parse_request(canonical(unknown))

        duplicate = canonical(self.request).replace(
            b'{"frame_range":', b'{"kind":"duplicate","frame_range":', 1
        )
        with self.assertRaisesRegex(contract_v2.ProjectContractError, "duplicate"):
            contract_v2.parse_request(duplicate)

        malformed = json.loads(json.dumps(self.request))
        malformed["resources"]["max_frames"] = True
        with self.assertRaisesRegex(contract_v2.ProjectContractError, "max_frames"):
            contract_v2.parse_request(canonical(malformed))

        raw_nan = canonical(self.request).replace(b'"width":64', b'"width":NaN')
        with self.assertRaisesRegex(contract_v2.ProjectContractError, "invalid JSON constant"):
            contract_v2.parse_request(raw_nan)

        malformed = json.loads(json.dumps(self.request))
        malformed["output"]["passes"] = [{}]
        with self.assertRaisesRegex(contract_v2.ProjectContractError, "passes"):
            contract_v2.parse_request(canonical(malformed))

    def test_feature_matrix_rejects_unsupported_or_semantically_changed_cells(self) -> None:
        mutations = [
            ("unsupported", lambda row: row["render"].__setitem__("feature_cell", "cuda_anything")),
            ("device", lambda row: row["render"].__setitem__("device", "METAL")),
            ("use_compositor", lambda row: row["output"].__setitem__("use_compositor", True)),
            ("passes", lambda row: row["output"].__setitem__("passes", ["Combined", "Depth"])),
            ("file_format", lambda row: row["output"].__setitem__("file_format", "OPEN_EXR")),
            ("use_motion_blur", lambda row: row["render"].__setitem__("use_motion_blur", True)),
        ]
        for expected, mutate in mutations:
            with self.subTest(expected=expected):
                request = json.loads(json.dumps(self.request))
                mutate(request)
                with self.assertRaisesRegex(contract_v2.ProjectContractError, expected):
                    contract_v2.parse_request(canonical(request))

        exr = json.loads(json.dumps(self.request))
        exr["render"]["feature_cell"] = "cycles_metal_openexr_rgba16_combined_no_post_v1"
        exr["render"]["device"] = "METAL"
        exr["output"].update(
            {"file_format": "OPEN_EXR", "color_depth": "16", "codec": "ZIP"}
        )
        contract_v2.parse_request(canonical(exr))

    def test_fallback_verifier_runtime_and_object_authority_cannot_be_weakened(self) -> None:
        mutations = [
            (
                "fallback_required",
                lambda row: row["policies"].__setitem__("fallback_required", False),
            ),
            ("fail_closed", lambda row: row["policies"].__setitem__("fail_closed", False)),
            (
                "quality_tier",
                lambda row: row["policies"].__setitem__(
                    "requested_quality_tier", "preview"
                ),
            ),
            (
                "server-selected",
                lambda row: row["verifier"].__setitem__("server_selected", False),
            ),
            (
                "server-selected",
                lambda row: row["verifier"].__setitem__(
                    "independent_reference_required", False
                ),
            ),
            ("lowercase SHA", lambda row: row["runtime"].__setitem__("blender_sha256", "A" * 64)),
            (
                "object_version",
                lambda row: row["project_object"].__setitem__(
                    "object_version", "version with spaces"
                ),
            ),
            (
                "object_key",
                lambda row: row["project_object"].__setitem__(
                    "object_key", "../escaped.tar.zst"
                ),
            ),
        ]
        for expected, mutate in mutations:
            with self.subTest(expected=expected):
                request = json.loads(json.dumps(self.request))
                mutate(request)
                with self.assertRaisesRegex(contract_v2.ProjectContractError, expected):
                    contract_v2.parse_request(canonical(request))

    def test_frames_samples_and_resource_arithmetic_fail_closed(self) -> None:
        mutations = [
            ("frame_range", "count", 2, "arithmetic"),
            ("render", "draft_samples", 64, "sample ladder"),
            ("resources", "max_frames", 2, "max_frames"),
            ("resources", "max_pixels_per_frame", 4095, "max_pixels"),
            ("resources", "max_total_pixel_frames", 8192, "max_total"),
            ("resources", "max_output_bytes", 1 << 20, "max_output_bytes"),
            ("resources", "max_disk_bytes", 32 << 20, "max_disk_bytes"),
            ("resources", "max_project_files", 1, "max_project_files"),
            (
                "resources",
                "max_project_directories",
                0,
                "max_project_directories",
            ),
        ]
        for section, field, value, expected in mutations:
            with self.subTest(section=section, field=field):
                request = json.loads(json.dumps(self.request))
                request[section][field] = value
                raw = canonical(request)
                if section in {"frame_range", "render"}:
                    with self.assertRaisesRegex(contract_v2.ProjectContractError, expected):
                        contract_v2.parse_request(raw)
                else:
                    parsed = contract_v2.parse_request(raw)
                    with self.assertRaisesRegex(contract_v2.ProjectContractError, expected):
                        contract_v2._load_verified_inputs(
                            self.root, self.manifest_raw, self.object, parsed
                        )

    def test_project_directory_limit_binds_zero_and_rejects_invalid_ceilings(self) -> None:
        flat_root = self.base / "flat-project"
        flat_root.mkdir()
        (flat_root / "scene.blend").write_bytes(b"BLENDER-v400flat project")
        flat_manifest = bundle_v1.build_manifest(flat_root, "scene.blend")
        self.assertEqual(flat_manifest["directory_count"], 0)
        flat_manifest_raw = bundle_v1._canonical_json(flat_manifest) + b"\n"

        request = json.loads(json.dumps(self.request))
        request["resources"]["max_project_directories"] = 0
        request_raw = canonical(request)
        contract = contract_v2.build_contract(
            flat_root, flat_manifest_raw, self.object, request_raw
        )
        self.assertEqual(contract["project"]["directory_count"], 0)
        self.assertEqual(contract["resources"]["max_project_directories"], 0)
        self.assertEqual(
            contract_v2.verify_contract(
                flat_root,
                flat_manifest_raw,
                self.object,
                request_raw,
                canonical(contract),
            ),
            contract,
        )

        for invalid in (-1, bundle_v1.MAX_DIRECTORIES, True):
            with self.subTest(invalid=invalid):
                malformed = json.loads(json.dumps(self.request))
                malformed["resources"]["max_project_directories"] = invalid
                with self.assertRaisesRegex(
                    contract_v2.ProjectContractError, "max_project_directories"
                ):
                    contract_v2.parse_request(canonical(malformed))

    def test_object_and_bundle_bytes_are_replayed_not_merely_declared(self) -> None:
        contract = self.build()
        self.object.write_bytes(b"different object bytes")
        with self.assertRaisesRegex(contract_v2.ProjectContractError, "contradict"):
            self.verify(contract)

        self.object.write_bytes(b"sealed project-object fixture")
        contract = self.build()
        (self.root / "textures" / "hero.exr").write_bytes(b"changed texture")
        with self.assertRaisesRegex(contract_v2.ProjectContractError, "bundle manifest"):
            self.verify(contract)

    def test_object_symlinks_and_hardlinks_are_not_identity_authority(self) -> None:
        original = self.object
        link = self.base / "object-link"
        try:
            link.symlink_to(original)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaisesRegex(contract_v2.ProjectContractError, "cannot open"):
            contract_v2.build_contract(
                self.root, self.manifest_raw, link, self.request_raw
            )
        link.unlink()

        hardlink = self.base / "object-hardlink"
        try:
            os.link(original, hardlink)
        except OSError as exc:
            self.skipTest(f"hard links unavailable: {exc}")
        with self.assertRaisesRegex(contract_v2.ProjectContractError, "non-hard-linked"):
            self.build()

    def test_contract_tampering_fails_even_when_attacker_recomputes_local_hashes(self) -> None:
        contract = self.build()
        authorization = json.loads(json.dumps(contract))
        authorization["authorization"]["billing_eligible"] = True
        authorization["contract_sha256"] = contract_v2._contract_sha256(authorization)
        with self.assertRaisesRegex(contract_v2.ProjectContractError, "wire-only"):
            contract_v2.parse_contract(canonical(authorization))

        preflight = json.loads(json.dumps(contract))
        preflight["preflight"]["object_extraction_verified"] = True
        preflight["contract_sha256"] = contract_v2._contract_sha256(preflight)
        with self.assertRaisesRegex(contract_v2.ProjectContractError, "unsupported completion"):
            contract_v2.parse_contract(canonical(preflight))

        legacy_preflight = json.loads(json.dumps(contract))
        legacy_preflight["preflight"]["feature_cell_validated"] = legacy_preflight[
            "preflight"
        ].pop("request_feature_cell_schema_validated")
        legacy_preflight["contract_sha256"] = contract_v2._contract_sha256(
            legacy_preflight
        )
        with self.assertRaisesRegex(contract_v2.ProjectContractError, "unsupported completion"):
            contract_v2.parse_contract(canonical(legacy_preflight))

        version = json.loads(json.dumps(contract))
        version["project"]["object_version"] = "attacker-version"
        forged_request = contract_v2._request_from_contract(version)
        version["request_sha256"] = contract_v2._request_sha256(forged_request)
        version["contract_sha256"] = contract_v2._contract_sha256(version)
        with self.assertRaisesRegex(contract_v2.ProjectContractError, "original request"):
            self.verify(version)

        runtime = json.loads(json.dumps(contract))
        runtime["runtime"]["executor_sha256"] = "0" * 64
        forged_request = contract_v2._request_from_contract(runtime)
        runtime["request_sha256"] = contract_v2._request_sha256(forged_request)
        runtime["contract_sha256"] = contract_v2._contract_sha256(runtime)
        with self.assertRaisesRegex(contract_v2.ProjectContractError, "original request"):
            self.verify(runtime)

        for field, value in (
            ("manifest_sha256", "1" * 64),
            ("bundle_sha256", "2" * 64),
            ("scene_sha256", "3" * 64),
            ("scene_path", "other.blend"),
            ("directory_count", self.manifest["directory_count"] + 1),
        ):
            with self.subTest(project_field=field):
                project = json.loads(json.dumps(contract))
                project["project"][field] = value
                project["contract_sha256"] = contract_v2._contract_sha256(project)
                with self.assertRaisesRegex(
                    contract_v2.ProjectContractError, "verified inputs"
                ):
                    self.verify(project)

    def test_contract_parser_rejects_unknown_duplicate_and_invalid_self_hash(self) -> None:
        contract = self.build()
        unknown = dict(contract)
        unknown["surprise"] = True
        with self.assertRaisesRegex(contract_v2.ProjectContractError, "unknown or missing"):
            contract_v2.parse_contract(canonical(unknown))

        duplicate = canonical(contract).replace(
            b'{"authorization":', b'{"kind":"duplicate","authorization":', 1
        )
        with self.assertRaisesRegex(contract_v2.ProjectContractError, "duplicate"):
            contract_v2.parse_contract(duplicate)

        contract["resources"]["max_processes"] = 63
        with self.assertRaisesRegex(contract_v2.ProjectContractError, "self SHA"):
            contract_v2.parse_contract(canonical(contract))

    def test_cli_create_verify_no_clobber_and_outside_root_boundaries(self) -> None:
        manifest_path = self.base / "manifest.json"
        request_path = self.base / "request.json"
        contract_path = self.base / "contract.json"
        manifest_path.write_bytes(self.manifest_raw)
        request_path.write_bytes(self.request_raw)
        base_command = [
            sys.executable,
            str(TOOL),
            "--root",
            str(self.root),
            "--manifest",
            str(manifest_path),
            "--object",
            str(self.object),
            "--request",
            str(request_path),
        ]
        created = subprocess.run(
            [
                base_command[0],
                base_command[1],
                "create",
                *base_command[2:],
                "--output",
                str(contract_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(created.returncode, 0, created.stderr.decode())

        verified = subprocess.run(
            [
                base_command[0],
                base_command[1],
                "verify",
                *base_command[2:],
                "--contract",
                str(contract_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(verified.returncode, 0, verified.stderr.decode())
        proof = json.loads(verified.stdout)
        self.assertTrue(proof["ok"])
        self.assertEqual(proof["admission"], "wire_only")

        repeated = subprocess.run(
            [
                base_command[0],
                base_command[1],
                "create",
                *base_command[2:],
                "--output",
                str(contract_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(repeated.returncode, 2)
        self.assertIn(b"refusing to replace", repeated.stderr)

        inside = subprocess.run(
            [
                base_command[0],
                base_command[1],
                "create",
                *base_command[2:],
                "--output",
                str(self.root / "contract.json"),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(inside.returncode, 2)
        self.assertIn(b"outside", inside.stderr)


if __name__ == "__main__":
    unittest.main()
