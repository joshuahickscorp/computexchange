#!/usr/bin/env python3
"""Pure contract tests for the one-render confidence frontier screen."""

from __future__ import annotations

import copy
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import screen_blender_one_render_confidence as screen  # noqa: E402


def fixture_closed_report() -> dict[str, object]:
    sockets = [
        "Image",
        "Noisy Image",
        "Denoising Albedo",
        "Denoising Normal",
        "Denoising Depth",
        "Debug Sample Count",
    ]
    source_id = "a" * 64
    return {
        "kind": screen.KIND,
        "schema_version": screen.SCHEMA_VERSION,
        "render": {
            "source_render_id": source_id,
            "target_render_invocations": 1,
        },
        "stages": {
            name: {"source_render_id": source_id}
            for name in screen.CAPTURED_STAGES
        },
        "decision": {
            "confidence_is_independent": False,
            "independent_second_seed_rendered": False,
            "quality_authorized": False,
            "production_change_authorized": False,
        },
        "pass_manifest": {"ordered_socket_names": sockets},
        "pass_capabilities": screen.analyze_confidence_passes(sockets),
        "same_render_confidence": {
            "descriptive_only": True,
            "independent_verification": False,
            "quality_authorized": False,
        },
        "candidates": [
            {
                "name": "oidn_bicubic",
                "quality_audit_charged_to_candidate_wall": False,
            }
        ],
        "frontier_conclusion": {
            "same_render_confidence_can_replace_two_seed_verification": False
        },
        "finalization": {"closed": True},
    }


class OneRenderConfidenceTest(unittest.TestCase):
    def test_pass_analysis_distinguishes_guides_debug_and_true_variance(self) -> None:
        sockets = [
            "Image",
            "Noisy Image",
            "Denoising Albedo",
            "Denoising Normal",
            "Denoising Depth",
            "Debug Sample Count",
        ]
        result = screen.analyze_confidence_passes(sockets)
        self.assertTrue(result["debug_sample_count_available"])
        self.assertTrue(result["denoising_depth_available"])
        self.assertFalse(result["true_variance_pass_available"])
        self.assertEqual(result["true_variance_or_confidence_passes"], [])
        self.assertEqual(
            set(result["captured_confidence_like"]),
            {"Denoising Depth", "Debug Sample Count"},
        )

        with_variance = screen.analyze_confidence_passes(sockets + ["Variance"])
        self.assertTrue(with_variance["true_variance_pass_available"])
        self.assertEqual(with_variance["true_variance_or_confidence_passes"], ["Variance"])

    def test_same_render_confidence_is_descriptive_and_non_independent(self) -> None:
        y, x = np.mgrid[0:8, 0:12]
        denoised = np.zeros((8, 12, 4), dtype=np.uint8)
        denoised[..., 0] = x * 8
        denoised[..., 1] = y * 12
        denoised[..., 2] = 64
        denoised[..., 3] = 255
        noisy = denoised.copy()
        noisy[2:5, 3:7, :3] += 5
        albedo = np.stack((x / 11, y / 7, (x + y) / 18), axis=2)[..., :3]
        normal = np.dstack(
            (np.broadcast_to(x / 11, x.shape), np.broadcast_to(y / 7, y.shape), np.ones_like(x))
        )
        depth = np.dstack((x + y, x + y, x + y, np.ones_like(x))).astype(float)
        samples = np.full((8, 12, 4), 4.0, dtype=float)
        result = screen.same_render_confidence(
            denoised,
            noisy,
            albedo=albedo,
            normal=normal,
            depth=depth,
            sample_count=samples,
        )
        self.assertTrue(result["confidence_gate_pass"])
        self.assertTrue(result["descriptive_only"])
        self.assertFalse(result["independent_verification"])
        self.assertFalse(result["quality_authorized"])
        self.assertTrue(result["same_render_sample_reuse"])

        varying_samples = samples.copy()
        varying_samples[0, 0, 0] = 1.0
        rejected = screen.same_render_confidence(
            denoised,
            noisy,
            albedo=albedo,
            normal=normal,
            depth=depth,
            sample_count=varying_samples,
        )
        self.assertFalse(rejected["confidence_gate_pass"])
        self.assertFalse(rejected["checks"]["sample_count_range"]["pass"])

    def test_candidate_reconstruction_is_bounded_and_exact(self) -> None:
        denoised = np.full((4, 6, 4), 100, dtype=np.uint8)
        denoised[..., 3] = 231
        noisy = np.full((4, 6, 4), 200, dtype=np.uint8)
        noisy[..., 3] = 17
        edge = np.zeros((4, 6), dtype=float)
        edge[:, 3:] = 1.0

        plain = screen._candidate_lowres(
            denoised,
            noisy,
            {"noisy_fraction": 0.10},
            edge,
        )
        self.assertTrue(np.all(plain[..., :3] == 110))
        self.assertTrue(np.all(plain[..., 3] == 231))

        guided = screen._candidate_lowres(
            denoised,
            noisy,
            {"noisy_fraction": 0.10, "guide_edge_weighted": True},
            edge,
        )
        self.assertTrue(np.all(guided[:, :3, :3] == 100))
        self.assertTrue(np.all(guided[:, 3:, :3] == 110))

    def test_render_and_closed_report_validators_never_broaden_confidence(self) -> None:
        report = fixture_closed_report()
        screen.validate_render_report(report)
        screen.validate_closed_report(report)

        changed = copy.deepcopy(report)
        changed["decision"]["confidence_is_independent"] = True
        with self.assertRaisesRegex(screen.OneRenderError, "claim broadened"):
            screen.validate_closed_report(changed)

        changed = copy.deepcopy(report)
        changed["frontier_conclusion"][
            "same_render_confidence_can_replace_two_seed_verification"
        ] = True
        with self.assertRaisesRegex(screen.OneRenderError, "replaced independent"):
            screen.validate_closed_report(changed)

        changed = copy.deepcopy(report)
        changed["render"]["target_render_invocations"] = 2
        with self.assertRaisesRegex(screen.OneRenderError, "exactly one target"):
            screen.validate_closed_report(changed)

    def test_cli_is_bounded_to_frozen_koro_arms(self) -> None:
        command, args = screen._parse_outer(
            [
                "render",
                "--scene",
                "/tmp/main.blend",
                "--output-dir",
                "/tmp/new-output",
                "--width",
                "810",
                "--height",
                "1440",
                "--frame",
                "9",
                "--samples",
                "4",
                "--device",
                "METAL",
            ]
        )
        self.assertEqual(command, "render")
        self.assertEqual((args.width, args.height, args.samples), (810, 1440, 4))

        with self.assertRaisesRegex(screen.OneRenderError, "render size"):
            screen._parse_outer(
                [
                    "render",
                    "--scene",
                    "/tmp/main.blend",
                    "--output-dir",
                    "/tmp/new-output",
                    "--width",
                    "800",
                    "--height",
                    "1400",
                    "--frame",
                    "9",
                    "--samples",
                    "4",
                    "--device",
                    "METAL",
                ]
            )

        with self.assertRaisesRegex(screen.OneRenderError, "1/2/4 SPP"):
            screen._parse_outer(
                [
                    "render",
                    "--scene",
                    "/tmp/main.blend",
                    "--output-dir",
                    "/tmp/new-output",
                    "--width",
                    "810",
                    "--height",
                    "1440",
                    "--frame",
                    "9",
                    "--samples",
                    "3",
                    "--device",
                    "METAL",
                ]
            )

        with tempfile.TemporaryDirectory() as temporary:
            relative = Path(temporary).name
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    screen._parse_outer(
                        [
                            "finalize",
                            "--render-report",
                            relative,
                            "--reference",
                            "/tmp/reference.png",
                            "--baseline-s",
                            "112.396726",
                        ]
                    )

    def test_candidate_spec_count_is_bounded_and_source_delegates_one_render(self) -> None:
        self.assertLessEqual(len(screen.CANDIDATE_SPECS), screen.MAX_CANDIDATES)
        self.assertEqual(len(screen.SWEEP_FRACTIONS), screen.MAX_CANDIDATES)
        self.assertEqual(tuple(sorted(screen.SWEEP_FRACTIONS)), screen.SWEEP_FRACTIONS)
        source = (HERE / "screen_blender_one_render_confidence.py").read_text()
        self.assertIn("report = base._run_blender(args)", source)
        self.assertNotIn("bpy.ops.render.render", source)
        self.assertIn("confidence_is_independent", source)

    def test_cpu_sweep_report_is_bounded_and_never_authorizes_confidence(self) -> None:
        policy = {
            "alpha_resample": "BICUBIC",
            "fractions": list(screen.SWEEP_FRACTIONS),
            "independent_verification": False,
            "maximum_candidates": screen.MAX_CANDIDATES,
            "rgb_resample": "BICUBIC",
            "same_render_pair_only": True,
        }
        report = {
            "kind": "cx_one_render_cpu_residual_sweep",
            "schema_version": 1,
            "closed": True,
            "source": {"target_render_invocations": 1},
            "policy": policy,
            "policy_sha256": screen.base._canonical_sha256(policy),
            "candidates": [
                {
                    "quality_v3_pass": index >= 4,
                    "quality_audit_charged_to_candidate_wall": False,
                }
                for index in range(screen.MAX_CANDIDATES)
            ],
            "conclusion": {
                "pass_count": 4,
                "same_render_confidence_can_authorize_quality": False,
            },
        }
        screen.validate_sweep_report(report)

        changed = copy.deepcopy(report)
        changed["source"]["target_render_invocations"] = 2
        with self.assertRaisesRegex(screen.OneRenderError, "one target render"):
            screen.validate_sweep_report(changed)

        changed = copy.deepcopy(report)
        changed["conclusion"]["same_render_confidence_can_authorize_quality"] = True
        with self.assertRaisesRegex(screen.OneRenderError, "conclusion changed"):
            screen.validate_sweep_report(changed)

        command, args = screen._parse_outer(
            [
                "sweep",
                "--render-report",
                "/tmp/closed.json",
                "--output-dir",
                "/tmp/new-sweep",
            ]
        )
        self.assertEqual(command, "sweep")
        self.assertEqual(args.output_dir, Path("/tmp/new-sweep"))


if __name__ == "__main__":
    unittest.main()
