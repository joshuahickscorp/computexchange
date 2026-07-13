#!/usr/bin/env python3
"""Pure host-side tests for the in-memory Blender endpoint screen."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

from PIL import Image


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import screen_blender_inmemory_endpoints as screen  # noqa: E402


class InMemoryEndpointScreenTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_png_identity_binds_hash_dimensions_and_mode(self) -> None:
        path = self.root / "rgba.png"
        Image.new("RGBA", (screen.WIDTH, screen.HEIGHT), (1, 2, 3, 4)).save(
            path, compress_level=0
        )
        identity = screen._png_identity(
            path, width=screen.WIDTH, height=screen.HEIGHT
        )
        self.assertEqual(identity["width"], screen.WIDTH)
        self.assertEqual(identity["height"], screen.HEIGHT)
        self.assertEqual(identity["color_type"], 6)
        self.assertEqual(
            identity["sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
        )
        with self.assertRaisesRegex(screen.ScreenError, "dimensions"):
            screen._png_identity(
                path, width=screen.WIDTH - 1, height=screen.HEIGHT
            )

    def test_output_and_json_publication_are_no_clobber(self) -> None:
        output = screen._prepare_output_root(self.root / "new-output")
        report = output / "report.json"
        screen._write_new_json(report, {"finite": 1.0})
        self.assertEqual(json.loads(report.read_text()), {"finite": 1.0})
        with self.assertRaises(screen.ScreenError):
            screen._write_new_json(report, {"finite": 2.0})
        with self.assertRaises(screen.ScreenError):
            screen._prepare_output_root(output)

    def test_quality_summary_selects_decision_metrics(self) -> None:
        metric = {"value": 0.95, "pass": True, "minimum": 0.94}
        result = {
            "alpha_agreement": metric,
            "errors": [],
            "failures": [],
            "mattes": {
                "black": {
                    "metrics": {
                        "global_rgb_agreement": metric,
                        "gaussian_luma_ssim": metric,
                    }
                }
            },
            "pass": True,
        }
        summary = screen._quality_summary(result)
        self.assertTrue(summary["pass"])
        self.assertEqual(
            summary["metrics_black_matte"]["global_rgb_agreement"],
            {"value": 0.95, "pass": True},
        )
        self.assertEqual(
            summary["metrics_black_matte"]["gaussian_luma_ssim"],
            {"value": 0.95, "pass": True},
        )

    def test_private_output_names_are_flat_and_closed(self) -> None:
        output = screen._prepare_output_root(self.root / "artifacts")
        self.assertEqual(
            screen._safe_output_path(output, "cycles-render-result-s4.png"),
            output / "cycles-render-result-s4.png",
        )
        for bad in ("../escape.png", "nested/output.png", "UPPER.png", ""):
            with self.subTest(name=bad):
                with self.assertRaises(screen.ScreenError):
                    screen._safe_output_path(output, bad)

    def test_blender_cli_arguments_are_sliced_after_separator(self) -> None:
        self.assertEqual(
            screen._cli_argv(
                [
                    "--background",
                    "--python",
                    "screen.py",
                    "--",
                    "--child",
                    "--scene",
                    "/tmp/main.blend",
                ]
            ),
            ["--child", "--scene", "/tmp/main.blend"],
        )


if __name__ == "__main__":
    unittest.main()
