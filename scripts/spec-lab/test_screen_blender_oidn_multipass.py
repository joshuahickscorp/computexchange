#!/usr/bin/env python3
"""Pure tests for the experimental Blender OIDN multipass screen."""

from __future__ import annotations

import copy
import contextlib
import hashlib
import io
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zlib


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import screen_blender_oidn_multipass as screen  # noqa: E402


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def png_bytes(
    width: int,
    height: int,
    pixels: bytes,
    *,
    color_type: int = 6,
    bit_depth: int = 8,
    interlace: int = 0,
) -> bytes:
    channels = 4 if color_type == 6 else 3
    stride = width * channels
    if len(pixels) != stride * height:
        raise AssertionError("fixture pixels have wrong length")
    raw = b"".join(
        b"\x00" + pixels[row * stride : (row + 1) * stride]
        for row in range(height)
    )
    header = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, interlace)
    return (
        screen.PNG_SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


DENOISED = bytes(
    [
        10,
        20,
        30,
        255,
        50,
        60,
        70,
        128,
        90,
        100,
        110,
        0,
        130,
        140,
        150,
        255,
    ]
)
NOISY = bytes(
    [
        10,
        20,
        30,
        255,
        45,
        65,
        70,
        128,
        100,
        100,
        100,
        0,
        130,
        140,
        150,
        254,
    ]
)


def fixture_report() -> dict[str, object]:
    ordered = [
        "Image",
        "Alpha",
        "Noisy Image",
        "Denoising Normal",
        "Denoising Albedo",
        "Denoising Depth",
    ]
    graph = {"links_sha256": "b" * 64}
    scene = {"sha256": "c" * 64}
    settings = {
        "device": "CPU",
        "enabled_device_names": ["CPU"],
        "frame": 1,
        "height": 2,
        "oidn_policy": dict(screen.OIDN_POLICY),
        "persistent_data": True,
        "sample_offset": 0,
        "sample_range": [0, 4],
        "samples": 4,
        "seed": 0,
        "use_adaptive_sampling": False,
        "view_layer": "ViewLayer",
        "width": 2,
    }
    settings_sha256 = screen._canonical_sha256(settings)
    pass_manifest = screen.analyze_pass_manifest(ordered)
    source_render_id = screen.source_render_id(
        graph_sha256=graph["links_sha256"],
        pass_manifest_sha256=pass_manifest["ordered_socket_names_sha256"],
        scene_sha256=scene["sha256"],
        settings_sha256=settings_sha256,
    )
    stages = {}
    for index, name in enumerate(("denoised", "noisy", "albedo", "normal")):
        stages[name] = {
            "channels": 4,
            "colorspace": "Linear Rec.709",
            "exr_bytes": 100,
            "exr_path": f"cx-{name}-0001.exr",
            "exr_sha256": format(index + 1, "x") * 64,
            "extraction_wall_s": 0.01,
            "float_count": 16,
            "float_rgba_sha256": format(index + 5, "x") * 64,
            "height": 2,
            "nonfinite_float_count": 0,
            "source_render_id": source_render_id,
            "width": 2,
        }
    outputs = {}
    for index, (name, pixels) in enumerate((("denoised", DENOISED), ("noisy", NOISY))):
        outputs[name] = {
            "bytes": 100,
            "decoded_rgba_sha256": hashlib.sha256(pixels).hexdigest(),
            "encoding_wall_s": 0.02,
            "path": f"{name}.png",
            "sha256": format(index + 9, "x") * 64,
            "source_render_id": source_render_id,
            "strict_png": {
                "bit_depth": 8,
                "channels": 4,
                "chunk_types": ["IHDR", "IDAT", "IEND"],
                "color_type": "RGBA",
                "height": 2,
                "interlaced": False,
                "strict_8bit_rgba": True,
                "width": 2,
            },
        }
    diagnostic = screen.reference_free_diagnostic(DENOISED, NOISY)
    diagnostic["wall_s"] = 0.001
    return {
        "blender": {},
        "configuration": {
            "compositor_graph": graph,
            "exact_target_settings": settings,
            "exact_target_settings_sha256": settings_sha256,
        },
        "decision": {
            "experimental_feasible": True,
            "guide_passes_extracted": True,
            "production_change_authorized": False,
            "quality_authorized": False,
            "same_render_pair_extracted": True,
        },
        "experimental_only": True,
        "host": {},
        "kind": screen.KIND,
        "limitations": [],
        "outputs": outputs,
        "pass_manifest": pass_manifest,
        "reference_free_diagnostic": diagnostic,
        "render": {
            "source_render_id": source_render_id,
            "target_render_and_exr_staging_wall_s": 0.2,
            "target_render_invocations": 1,
            "target_timer_includes_exr_staging": True,
            "warmup_render_invocations": 1,
            "warmup_wall_s": 0.1,
        },
        "scene": scene,
        "schema_version": screen.SCHEMA_VERSION,
        "stages": stages,
    }


class OIDNMultipassScreenTest(unittest.TestCase):
    def test_pass_manifest_requires_noisy_denoised_and_both_guides(self) -> None:
        complete = screen.analyze_pass_manifest(screen.REQUIRED_PASSES)
        self.assertTrue(complete["pair_extractable"])
        self.assertTrue(complete["guided_pair_extractable"])
        self.assertEqual(complete["missing_required"], [])

        no_normal = screen.analyze_pass_manifest(
            ["Image", "Noisy Image", "Denoising Albedo"]
        )
        self.assertTrue(no_normal["pair_extractable"])
        self.assertFalse(no_normal["guided_pair_extractable"])
        self.assertEqual(no_normal["missing_required"], ["Denoising Normal"])

    def test_strict_png_decoder_accepts_only_8bit_rgba(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "rgba.png"
            path.write_bytes(png_bytes(2, 2, DENOISED))
            decoded, info = screen.decode_strict_rgba_png(
                path, expected_width=2, expected_height=2
            )
            self.assertEqual(decoded, DENOISED)
            self.assertTrue(info["strict_8bit_rgba"])

            rgb = Path(temporary) / "rgb.png"
            rgb.write_bytes(png_bytes(1, 1, b"\x01\x02\x03", color_type=2))
            with self.assertRaisesRegex(screen.ScreenError, "not strict"):
                screen.decode_strict_rgba_png(rgb, expected_width=1, expected_height=1)

            corrupt = bytearray(path.read_bytes())
            corrupt[-1] ^= 1
            broken = Path(temporary) / "broken.png"
            broken.write_bytes(corrupt)
            with self.assertRaisesRegex(screen.ScreenError, "CRC mismatch"):
                screen.decode_strict_rgba_png(broken, expected_width=2, expected_height=2)

    def test_reference_free_diagnostic_describes_but_never_approves(self) -> None:
        diagnostic = screen.reference_free_diagnostic(DENOISED, NOISY)
        self.assertTrue(diagnostic["descriptive_only"])
        self.assertFalse(diagnostic["quality_authorized"])
        self.assertFalse(diagnostic["pixel_identical"])
        self.assertEqual(diagnostic["changed_pixels"], 2)
        self.assertEqual(diagnostic["max_abs_rgb_channel_change"], 10)
        self.assertLess(diagnostic["rgb_mae_agreement"], 1.0)

        identical = screen.reference_free_diagnostic(DENOISED, DENOISED)
        self.assertTrue(identical["pixel_identical"])
        self.assertEqual(identical["rgb_mae_agreement"], 1.0)
        self.assertFalse(identical["quality_authorized"])

    def test_report_validator_binds_every_artifact_to_one_target_render(self) -> None:
        report = fixture_report()
        screen.validate_report(report)

        changed = copy.deepcopy(report)
        changed["render"]["target_render_invocations"] = 2
        with self.assertRaisesRegex(screen.ScreenError, "exactly one target"):
            screen.validate_report(changed)

        changed = copy.deepcopy(report)
        changed["stages"]["noisy"]["source_render_id"] = "b" * 64
        with self.assertRaisesRegex(screen.ScreenError, "one target render"):
            screen.validate_report(changed)

        changed = copy.deepcopy(report)
        changed["scene"]["sha256"] = "d" * 64
        with self.assertRaisesRegex(screen.ScreenError, "does not bind"):
            screen.validate_report(changed)

        changed = copy.deepcopy(report)
        changed["decision"]["quality_authorized"] = True
        with self.assertRaisesRegex(screen.ScreenError, "cannot authorize quality"):
            screen.validate_report(changed)

        changed = copy.deepcopy(report)
        changed["pass_manifest"]["ordered_socket_names"].remove("Denoising Normal")
        with self.assertRaisesRegex(screen.ScreenError, "not canonical"):
            screen.validate_report(changed)

    def test_cli_probe_is_write_free_and_measurement_is_bounded(self) -> None:
        probe = screen._parse_args(["--probe-only"])
        self.assertTrue(probe.probe_only)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                screen._parse_args(["--probe-only", "--width", "1"])

        args = screen._parse_args(
            [
                "--scene",
                "/tmp/scene.blend",
                "--output-dir",
                "/tmp/new-output",
                "--width",
                "512",
                "--height",
                "288",
                "--frame",
                "1",
                "--samples",
                "4",
                "--device",
                "CPU",
            ]
        )
        self.assertEqual(args.samples, 4)
        self.assertEqual(args.warmup_samples, 1)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                screen._parse_args(
                    [
                        "--scene",
                        "/tmp/scene.blend",
                        "--output-dir",
                        "/tmp/new-output",
                        "--width",
                        "4097",
                        "--height",
                        "4096",
                        "--frame",
                        "1",
                        "--samples",
                        "4",
                        "--device",
                        "CPU",
                    ]
                )

    def test_source_has_one_warmup_and_one_target_render_operator(self) -> None:
        source = (HERE / "screen_blender_oidn_multipass.py").read_text()
        self.assertEqual(source.count("bpy.ops.render.render(write_still=False"), 2)
        self.assertIn("target_render_invocations\": 1", source)
        self.assertIn("warmup_render_invocations\": 1", source)


if __name__ == "__main__":
    unittest.main()
