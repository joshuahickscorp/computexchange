#!/usr/bin/env python3
"""Tests for the fresh same-session Cycles benchmark harness."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_local_cycles_spec_benchmark as benchmark  # noqa: E402
from test_cx_cycles_render_preview_backend import write_fake_blender  # noqa: E402


class LocalCyclesSpecBenchmarkTest(unittest.TestCase):
    def test_oidn_candidate_profile_is_an_explicit_cli_choice(self) -> None:
        args = benchmark.parse_args(
            [
                "--scene",
                "/tmp/fixture.blend",
                "--candidate-profile",
                "oidn_native_v1",
                "--resident-policy",
                "same_frame_minimal_v1",
            ]
        )
        self.assertEqual(args.candidate_profile, "oidn_native_v1")
        self.assertEqual(args.resident_policy, "same_frame_minimal_v1")

    def test_fake_resident_renderer_emits_measured_comparable_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_raw:
            temp = Path(temp_raw)
            scene_root = temp / "scenes"
            scene_root.mkdir()
            scene = scene_root / "tiny.blend"
            scene.write_bytes(b"pinned fake blend")
            blender = temp / "fake-blender"
            write_fake_blender(blender, "accept")
            output_root = temp / "outputs"
            receipt_path = temp / "receipt.json"
            args = benchmark.parse_args(
                [
                    "--scene",
                    str(scene),
                    "--blender",
                    str(blender),
                    "--device",
                    "CPU",
                    "--allow-untrusted-renderer",
                    "--candidate-profile",
                    "oidn_native_v1",
                    "--resident-policy",
                    "same_frame_minimal_v1",
                    "--width",
                    "64",
                    "--height",
                    "64",
                    "--reference-samples",
                    "16",
                    "--draft-samples",
                    "4",
                    "--verify-samples",
                    "4",
                    "--timeout-secs",
                    "5",
                    "--output-root",
                    str(output_root),
                    "--json-out",
                    str(receipt_path),
                ]
            )
            result = benchmark.run_benchmark(args)

            self.assertEqual(result["evidence"], "synthetic")
            self.assertEqual(result["device"], "UNTRUSTED/CPU")
            self.assertEqual(result["resident_policy"], "same_frame_minimal_v1")
            self.assertEqual(
                result["worker_renderer_identity"]["resident_policy"],
                "same_frame_minimal_v1",
            )
            self.assertEqual(
                result["worker_renderer_identity"]["candidate_profile"]["name"],
                "oidn_native_v1",
            )
            self.assertTrue(result["cache_used"] is False)
            self.assertTrue(result["sample_ranges_disjoint"])
            self.assertEqual(result["warmup_candidate_runs"], 1)
            self.assertEqual(result["trial_count"], 1)
            self.assertIsNone(result["variance_estimate"])
            self.assertTrue(result["quality_gate"])
            self.assertEqual(result["global_agreement"], 1.0)
            self.assertEqual(result["worst_tile_agreement"], 1.0)
            self.assertGreater(result["baseline_s"], 0.0)
            self.assertGreater(result["spec_s"], 0.0)
            self.assertIsNotNone(result["speedup_x"])
            self.assertEqual(
                result["controller_receipt"]["evidence"], "synthetic"
            )
            self.assertTrue(result["benchmark_audit"]["passed"])
            self.assertFalse(result["reference_used_for_product_decision"])
            self.assertFalse(result["meets_50x_preview_experiment"])
            self.assertNotIn("meets_50x_goal", result)
            self.assertEqual(
                result["pins"]["render_preview_driver_sha256"],
                benchmark.sha256_file(benchmark.DRIVER_PATH),
            )
            self.assertTrue(receipt_path.is_file())
            self.assertEqual(json.loads(receipt_path.read_text()), result)

    def test_rejects_relative_scene_and_unsafe_sample_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_raw:
            temp = Path(temp_raw)
            scene = temp / "tiny.blend"
            scene.write_bytes(b"blend")
            blender = temp / "fake-blender"
            write_fake_blender(blender, "accept")

            relative = benchmark.parse_args(
                ["--scene", "tiny.blend", "--blender", str(blender)]
            )
            with self.assertRaisesRegex(ValueError, "absolute"):
                benchmark.validate_args(relative)

            bad_samples = benchmark.parse_args(
                [
                    "--scene",
                    str(scene),
                    "--blender",
                    str(blender),
                    "--reference-samples",
                    "4",
                    "--draft-samples",
                    "4",
                ]
            )
            with self.assertRaisesRegex(ValueError, "must exceed"):
                benchmark.validate_args(bad_samples)

    def test_json_output_is_no_clobber_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_raw:
            root = Path(temp_raw)
            target = root / "receipt.json"
            target.write_text("owned by caller", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                benchmark._write_json(target, {"ok": True}, force=False)
            self.assertEqual(target.read_text(), "owned by caller")

            victim = root / "victim.txt"
            victim.write_text("do not truncate", encoding="utf-8")
            symlink = root / "linked-receipt.json"
            try:
                symlink.symlink_to(victim)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            with self.assertRaises(FileExistsError):
                benchmark._write_json(symlink, {"ok": True}, force=False)
            self.assertEqual(victim.read_text(), "do not truncate")

    def test_preview_headline_requires_accepted_disjoint_draft(self) -> None:
        receipt = {"quality_gate": True}
        audit = {
            "passed": True,
            "sample_ranges_disjoint": True,
            "candidate": {"phase": "draft"},
        }
        self.assertTrue(
            benchmark._preview_experiment_meets(
                renderer_trusted=True,
                canonical_receipt=receipt,
                audit=audit,
                speedup=50.0,
                minimum=50.0,
            )
        )
        for update in (
            {"sample_ranges_disjoint": False},
            {"candidate": {"phase": "repair"}},
            {"passed": False},
        ):
            with self.subTest(update=update):
                self.assertFalse(
                    benchmark._preview_experiment_meets(
                        renderer_trusted=True,
                        canonical_receipt=receipt,
                        audit={**audit, **update},
                        speedup=100.0,
                        minimum=50.0,
                    )
                )


if __name__ == "__main__":
    unittest.main()
