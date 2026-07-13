#!/usr/bin/env python3
"""Contract, mutation, corpus, and CLI tests for render quality v3."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import BytesIO, StringIO
import json
import math
from pathlib import Path
import subprocess
import struct
import sys
import tempfile
import unittest
from unittest import mock
import zlib

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import gaussian_filter


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cx_render_quality_v3 as quality  # noqa: E402


WIDTH = 192
HEIGHT = 128


def rich_reference(width: int = WIDTH, height: int = HEIGHT) -> np.ndarray:
    """A deterministic display-code image with flat, edge, and fine-detail strata."""

    y, x = np.mgrid[0:height, 0:width]
    xf = x / max(1, width - 1)
    yf = y / max(1, height - 1)
    checker = ((x // 3 + y // 3) % 2).astype(np.float64)
    fine = np.sin(x * 0.91) * np.cos(y * 0.73)
    radial = np.hypot(xf - 0.63, yf - 0.46)
    subject = np.clip(1.0 - radial * 3.2, 0.0, 1.0)
    red = 0.10 + 0.55 * xf + 0.18 * checker + 0.12 * fine + 0.22 * subject
    green = 0.08 + 0.48 * yf + 0.14 * (1.0 - checker) - 0.08 * fine + 0.30 * subject
    blue = 0.12 + 0.35 * (1.0 - xf) + 0.16 * checker + 0.10 * fine + 0.12 * subject
    rgb = np.clip(np.stack((red, green, blue), axis=2), 0.0, 1.0)
    result = np.rint(rgb * 255.0).astype(np.uint8)

    # Stable line, grille, text-like, and highlight details used by deletions.
    image = Image.fromarray(result, "RGB")
    draw = ImageDraw.Draw(image)
    for column in range(7, width, 17):
        draw.line((column, 3, column, height - 4), fill=(242, 239, 225), width=1)
    for row in range(9, height, 19):
        draw.line((2, row, width - 3, row), fill=(12, 18, 26), width=1)
    for index in range(8):
        left = 12 + index * 12
        draw.rectangle((left, height - 27, left + 7, height - 21), fill=(248, 244, 234))
        draw.rectangle((left + 2, height - 20, left + 5, height - 15), fill=(15, 20, 30))
    draw.ellipse((width - 54, 18, width - 23, 49), fill=(250, 248, 239))
    draw.ellipse((width - 47, 25, width - 30, 42), fill=(26, 31, 38))
    return np.asarray(image, dtype=np.uint8)


def png_bytes(array: np.ndarray, mode: str | None = None) -> bytes:
    output = BytesIO()
    selected_mode = mode or ("RGBA" if array.shape[2] == 4 else "RGB")
    Image.fromarray(array, selected_mode).save(output, format="PNG", compress_level=0)
    return output.getvalue()


def blur(array: np.ndarray, sigma: float) -> np.ndarray:
    value = gaussian_filter(
        array.astype(np.float64) / 255.0,
        sigma=(sigma, sigma, 0.0),
        mode="reflect",
    )
    return np.rint(np.clip(value, 0.0, 1.0) * 255.0).astype(np.uint8)


def half_resolution(array: np.ndarray) -> np.ndarray:
    image = Image.fromarray(array, "RGB")
    reduced = image.resize((array.shape[1] // 2, array.shape[0] // 2), Image.Resampling.LANCZOS)
    return np.asarray(reduced.resize(image.size, Image.Resampling.LANCZOS), dtype=np.uint8)


def translate(array: np.ndarray, pixels: int) -> np.ndarray:
    result = np.empty_like(array)
    result[:, :pixels] = 0
    result[:, pixels:] = array[:, :-pixels]
    return result


def legacy_pass(candidate: np.ndarray, reference: np.ndarray) -> bool:
    candidate_f = candidate[..., :3].astype(np.float64) / 255.0
    reference_f = reference[..., :3].astype(np.float64) / 255.0
    global_score, regional, micro, _difference = quality._rgb_agreement_values(
        candidate_f, reference_f, (reference.shape[1], reference.shape[0])
    )
    return global_score >= 0.90 and regional >= 0.85 and micro >= 0.70


def audit(candidate: np.ndarray, reference: np.ndarray) -> dict[str, object]:
    return quality.evaluate_png_bytes(
        png_bytes(candidate),
        png_bytes(reference),
        target_size=(reference.shape[1], reference.shape[0]),
    )


class QualityV3CoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.reference = rich_reference()
        cls.reference_png = png_bytes(cls.reference)

    def test_identity_passes_with_complete_deterministic_wire_result(self) -> None:
        first = quality.evaluate_png_bytes(
            self.reference_png,
            self.reference_png,
            target_size=(WIDTH, HEIGHT),
        )
        second = quality.evaluate_png_bytes(
            self.reference_png,
            self.reference_png,
            target_size=(WIDTH, HEIGHT),
        )
        self.assertTrue(first["pass"])
        self.assertEqual(first, second)
        self.assertEqual(first["alpha_agreement"]["value"], 1.0)
        self.assertEqual(first["contract"]["id"], quality.CONTRACT_ID)
        self.assertRegex(first["contract_sha256"], r"^[0-9a-f]{64}$")
        runtime = first["runtime"]
        self.assertEqual(
            runtime["versions"],
            {
                "pillow": quality.PIL.__version__,
                "numpy": quality.np.__version__,
                "scipy": quality.scipy.__version__,
            },
        )
        self.assertRegex(runtime["metric_module_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(runtime["dependency_tree_sha256"], r"^[0-9a-f]{64}$")
        for matte in ("black", "white"):
            self.assertTrue(first["mattes"][matte]["reference_eligibility"]["pass"])
            self.assertTrue(first["mattes"][matte]["pass"])
            metrics = first["mattes"][matte]["metrics"]
            self.assertEqual(metrics["gaussian_luma_ssim"]["value"], 1.0)
            self.assertEqual(metrics["sobel_gms_mean"]["value"], 1.0)
            self.assertEqual(metrics["sobel_gmsd"]["value"], 0.0)
            self.assertEqual(metrics["haar_detail_cosine"]["value"], 1.0)
            self.assertEqual(metrics["haar_detail_rms_gain"]["value"], 1.0)

    def test_wire_rounding_is_the_gated_boundary(self) -> None:
        self.assertEqual(quality._wire(0.9399999996), 0.94)
        self.assertTrue(quality._minimum_metric(0.9399999996, 0.94)["pass"])
        self.assertEqual(quality._wire(0.9399999994), 0.939999999)
        self.assertFalse(quality._minimum_metric(0.9399999994, 0.94)["pass"])
        self.assertTrue(quality._maximum_metric(0.0300000004, 0.030)["pass"])
        self.assertFalse(quality._maximum_metric(0.0300000006, 0.030)["pass"])

    def test_stable_activity_ties_use_row_major_indices(self) -> None:
        tied = np.ones((7, 9), dtype=np.float64)
        order = quality._stable_activity_order(tied)
        np.testing.assert_array_equal(order, np.arange(tied.size))
        tied[3, 4] = 0.0
        order = quality._stable_activity_order(tied)
        self.assertEqual(int(order[0]), 3 * 9 + 4)
        np.testing.assert_array_equal(order[1:], np.delete(np.arange(tied.size), 3 * 9 + 4))

    def test_type7_quantile(self) -> None:
        self.assertEqual(quality._type7_quantile([0.0, 1.0], 0.05), 0.05)
        values = list(range(144))
        self.assertAlmostEqual(quality._type7_quantile(values, 0.05), 7.15)

    def test_blank_reference_is_ineligible_and_zero_denominators_fail_closed(self) -> None:
        blank = np.full_like(self.reference, 128)
        result = audit(blank, blank)
        self.assertFalse(result["pass"])
        for matte in ("black", "white"):
            block = result["mattes"][matte]
            self.assertFalse(block["reference_eligibility"]["pass"])
            self.assertFalse(block["pass"])
            self.assertTrue(block["unavailable_reasons"])

    def test_nonfinite_decoded_values_fail_closed(self) -> None:
        rgb = self.reference.astype(np.float64) / 255.0
        rgb[0, 0, 0] = np.nan
        alpha = np.ones((HEIGHT, WIDTH), dtype=np.float64)
        with mock.patch.object(
            quality,
            "_decode_png",
            return_value=(rgb, alpha, "RGB"),
        ):
            result = quality.evaluate_png_bytes(
                self.reference_png,
                self.reference_png,
                target_size=(WIDTH, HEIGHT),
            )
        self.assertFalse(result["pass"])
        self.assertEqual(result["errors"], ["decoded:nonfinite_values"])
        self.assertNotIn("NaN", json.dumps(result, allow_nan=False))


class StrictPngAndAlphaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.reference = rich_reference()
        self.valid = png_bytes(self.reference)

    def test_malformed_signature_crc_palette_depth_and_dimensions_reject(self) -> None:
        malformed = quality.evaluate_png_bytes(
            b"not a png", self.valid, target_size=(WIDTH, HEIGHT)
        )
        self.assertIn("candidate:invalid_png_signature", malformed["errors"])

        corrupted = bytearray(self.valid)
        idat = corrupted.index(b"IDAT")
        corrupted[idat + 4] ^= 1
        crc = quality.evaluate_png_bytes(
            bytes(corrupted), self.valid, target_size=(WIDTH, HEIGHT)
        )
        self.assertIn("candidate:png_crc_mismatch", crc["errors"])

        interlaced = bytearray(self.valid)
        ihdr_payload_offset = 8 + 8
        interlaced[ihdr_payload_offset + 12] = 1
        ihdr_type_offset = 8 + 4
        ihdr_crc_offset = ihdr_payload_offset + 13
        interlaced[ihdr_crc_offset : ihdr_crc_offset + 4] = struct.pack(
            ">I",
            zlib.crc32(
                bytes(
                    interlaced[
                        ihdr_type_offset : ihdr_payload_offset + 13
                    ]
                )
            )
            & 0xFFFFFFFF,
        )
        result = quality.evaluate_png_bytes(
            bytes(interlaced), self.valid, target_size=(WIDTH, HEIGHT)
        )
        self.assertIn("candidate:unsupported_png_method", result["errors"])

        palette = Image.fromarray(self.reference, "RGB").quantize(colors=32)
        value = BytesIO()
        palette.save(value, format="PNG")
        result = quality.evaluate_png_bytes(
            value.getvalue(), self.valid, target_size=(WIDTH, HEIGHT)
        )
        self.assertIn("candidate:png_color_type_not_rgb_or_rgba", result["errors"])

        grayscale16 = np.arange(WIDTH * HEIGHT, dtype=np.uint16).reshape(HEIGHT, WIDTH)
        value = BytesIO()
        Image.fromarray(grayscale16).save(value, format="PNG")
        result = quality.evaluate_png_bytes(
            value.getvalue(), self.valid, target_size=(WIDTH, HEIGHT)
        )
        self.assertTrue(
            any(
                error.startswith("candidate:png_")
                for error in result["errors"]
            )
        )

        result = quality.evaluate_png_bytes(
            self.valid, self.valid, target_size=(WIDTH + 1, HEIGHT)
        )
        self.assertEqual(
            result["errors"],
            [
                "candidate:png_dimension_mismatch",
                "reference:png_dimension_mismatch",
            ],
        )

    def test_hidden_rgb_under_matching_zero_alpha_is_ignored(self) -> None:
        alpha = np.full((HEIGHT, WIDTH), 255, dtype=np.uint8)
        alpha[30:70, 55:105] = 0
        reference = np.dstack((self.reference, alpha))
        candidate = reference.copy()
        candidate[30:70, 55:105, :3] = np.array([255, 0, 255], dtype=np.uint8)
        result = quality.evaluate_png_bytes(
            png_bytes(candidate),
            png_bytes(reference),
            target_size=(WIDTH, HEIGHT),
        )
        self.assertTrue(result["pass"])
        self.assertEqual(result["alpha_agreement"]["value"], 1.0)

    def test_alpha_corruption_and_revealed_hidden_rgb_reject(self) -> None:
        alpha = np.full((HEIGHT, WIDTH), 255, dtype=np.uint8)
        alpha[30:70, 55:105] = 0
        reference = np.dstack((self.reference, alpha))
        candidate = reference.copy()
        candidate[30:70, 55:105, :3] = np.array([255, 0, 255], dtype=np.uint8)
        candidate[30:70, 55:105, 3] = 32
        result = quality.evaluate_png_bytes(
            png_bytes(candidate),
            png_bytes(reference),
            target_size=(WIDTH, HEIGHT),
        )
        self.assertFalse(result["pass"])
        self.assertFalse(result["alpha_agreement"]["pass"])
        self.assertIn("alpha_agreement", result["failures"])


class AdversarialMutationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.reference = rich_reference()

    def _assert_rejected(self, candidate: np.ndarray, label: str) -> dict[str, object]:
        result = audit(candidate, self.reference)
        self.assertFalse(result["pass"], label)
        return result

    def test_gaussian_blur_sigma_2_and_4_reject(self) -> None:
        for sigma in (2.0, 4.0):
            with self.subTest(sigma=sigma):
                self._assert_rejected(blur(self.reference, sigma), f"blur-{sigma}")

    def test_seeded_global_and_reference_flat_noise_reject(self) -> None:
        rng = np.random.default_rng(20260712)
        source = self.reference.astype(np.float64) / 255.0
        global_noise = np.clip(source + rng.normal(0.0, 0.075, source.shape), 0.0, 1.0)
        self._assert_rejected(
            np.rint(global_noise * 255.0).astype(np.uint8), "global-noise"
        )

        luma = quality._luma(source)
        _gx, _gy, magnitude = quality._sobel_components(luma)
        order = quality._stable_activity_order(magnitude)
        selected = order[: order.size // 2]
        flat = source.copy().reshape(-1, 3)
        flat[selected] = np.clip(
            flat[selected] + rng.normal(0.0, 0.14, (selected.size, 3)),
            0.0,
            1.0,
        )
        self._assert_rejected(
            np.rint(flat.reshape(source.shape) * 255.0).astype(np.uint8),
            "flat-noise",
        )

    def test_blur_plus_noise_cannot_spoof_gradient_energy(self) -> None:
        rng = np.random.default_rng(44)
        smooth = blur(self.reference, 2.0).astype(np.float64) / 255.0
        spoof = np.clip(smooth + rng.normal(0.0, 0.035, smooth.shape), 0.0, 1.0)
        result = self._assert_rejected(
            np.rint(spoof * 255.0).astype(np.uint8), "blur-noise-spoof"
        )
        # A plausible total-energy ratio does not authorize uncorrelated detail.
        black = result["mattes"]["black"]["metrics"]
        self.assertTrue(
            not black["haar_detail_cosine"]["pass"]
            or not black["flat_high_pass_rmse"]["pass"]
            or not black["sobel_gms_mean"]["pass"]
        )

    def test_half_resolution_with_and_without_post_gaussian_reject(self) -> None:
        half = half_resolution(self.reference)
        self._assert_rejected(half, "half-resolution")
        self._assert_rejected(blur(half, 0.3), "half-resolution-gaussian")

    def test_distributed_detail_deletions_reject(self) -> None:
        smooth = blur(self.reference, 3.0)
        masks: dict[str, np.ndarray] = {}
        y, x = np.mgrid[0:HEIGHT, 0:WIDTH]
        masks["thin_lines"] = (x % 17 == 7) | (y % 19 == 9)
        masks["grille"] = (x < WIDTH // 2) & ((x % 6 == 0) | (y % 8 == 0))
        masks["text"] = (y >= HEIGHT - 40) & (x < 160)
        reference_luma = quality._luma(self.reference.astype(np.float64) / 255.0)
        masks["highlight"] = reference_luma > 0.72
        masks["subject"] = ((x - int(WIDTH * 0.63)) ** 2 + (y - int(HEIGHT * 0.46)) ** 2) < 27**2
        for label, mask in masks.items():
            candidate = self.reference.copy()
            candidate[mask] = smooth[mask]
            with self.subTest(detail=label):
                self._assert_rejected(candidate, label)

    def test_one_two_four_eight_pixel_wide_defects_are_monotone(self) -> None:
        global_scores: list[float] = []
        rejected: list[bool] = []
        for width in (1, 2, 4, 8):
            candidate = self.reference.copy()
            left = WIDTH // 2 - width // 2
            candidate[:, left : left + width] = 0
            result = audit(candidate, self.reference)
            global_scores.append(
                result["mattes"]["black"]["metrics"]["global_rgb_agreement"]["value"]
            )
            rejected.append(not result["pass"])
        self.assertEqual(global_scores, sorted(global_scores, reverse=True))
        self.assertTrue(rejected[-1])

    def test_one_pixel_translation_rejects(self) -> None:
        result = self._assert_rejected(translate(self.reference, 1), "translation-1")
        metrics = result["mattes"]["black"]["metrics"]
        self.assertTrue(
            not metrics["sobel_gms_mean"]["pass"]
            or not metrics["haar_detail_cosine"]["pass"]
            or not metrics["regional_ssim_p5"]["pass"]
        )

    def test_eight_mutation_corpus_legacy_passes_v3_rejects(self) -> None:
        rng = np.random.default_rng(90125)
        source = self.reference.astype(np.float64) / 255.0
        half = half_resolution(self.reference)
        smooth = blur(self.reference, 3.0)
        y, x = np.mgrid[0:HEIGHT, 0:WIDTH]
        detail_mask = ((x % 17 == 7) | (y % 19 == 9) | (((x - 121) ** 2 + (y - 59) ** 2) < 22**2))
        deleted = self.reference.copy()
        deleted[detail_mask] = smooth[detail_mask]
        noise = np.rint(
            np.clip(source + rng.normal(0.0, 0.06, source.shape), 0.0, 1.0) * 255.0
        ).astype(np.uint8)
        spoof_source = blur(self.reference, 2.0).astype(np.float64) / 255.0
        spoof = np.rint(
            np.clip(spoof_source + rng.normal(0.0, 0.03, source.shape), 0.0, 1.0) * 255.0
        ).astype(np.uint8)

        def blend(mutation: np.ndarray, amount: float) -> np.ndarray:
            return np.rint(
                self.reference.astype(np.float64) * (1.0 - amount)
                + mutation.astype(np.float64) * amount
            ).astype(np.uint8)

        corpus = {
            "blur2": blend(blur(self.reference, 2.0), 0.25),
            "blur4": blend(blur(self.reference, 4.0), 0.25),
            "noise": noise,
            "blur_noise": blend(spoof, 0.20),
            "half": half,
            "half_gaussian": blur(half, 0.3),
            "detail_delete": deleted,
            "translation": blend(translate(self.reference, 1), 0.20),
        }
        for label, candidate in corpus.items():
            with self.subTest(mutation=label):
                self.assertTrue(legacy_pass(candidate, self.reference), label)
                self.assertFalse(audit(candidate, self.reference)["pass"], label)


class CliAndRetainedCorpusTest(unittest.TestCase):
    def test_cli_json_is_deterministic_and_exit_status_tracks_contract(self) -> None:
        reference = rich_reference()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate.png"
            high = root / "reference.png"
            candidate.write_bytes(png_bytes(reference))
            high.write_bytes(png_bytes(reference))
            command = [
                sys.executable,
                str(HERE / "cx_render_quality_v3.py"),
                str(candidate),
                str(high),
                "--width",
                str(WIDTH),
                "--height",
                str(HEIGHT),
            ]
            first = subprocess.run(command, check=False, capture_output=True, text=True)
            second = subprocess.run(command, check=False, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        payload = json.loads(first.stdout)
        self.assertTrue(payload["pass"])
        self.assertEqual(first.stderr, "")

    def test_retained_pavilion_passes_and_koro_legacy_passes_v3_rejects(self) -> None:
        pavilion = Path(
            "/Users/scammermike/.cache/cx-spec-lab/transfer/"
            "pavilion-v2-1080p-r4096-d24-v24-f1-20260712/"
            "cycles-preview-5e8d979d0422d339deb035aec7c98c1c/units/"
            "unit-57472ac5240030d509a3dd88304fc1a3e28526e24abb5db16a697662b574a062"
        )
        koro = Path(
            "/Users/scammermike/.cache/cx-spec-lab/transfer/"
            "koro-portrait-1080x1920-r4096-d4-v4-f9-20260712/"
            "cycles-preview-4c50873d7e61bbd6b6afdc10913938de/units/"
            "unit-20b9601a46a3394b1f207f0d9a8e2abb2bf2af4267233bcb2bdd03b6649e901a"
        )
        if not all(
            path.is_file()
            for path in (
                pavilion / "draft.png",
                pavilion / "baseline.png",
                koro / "draft.png",
                koro / "baseline.png",
            )
        ):
            self.skipTest("retained render corpus is not available")
        pavilion_result = quality.evaluate_pngs(
            pavilion / "draft.png",
            pavilion / "baseline.png",
            target_size=(1920, 1080),
        )
        self.assertTrue(pavilion_result["pass"])
        pavilion_metrics = pavilion_result["mattes"]["black"]["metrics"]
        self.assertEqual(pavilion_metrics["gaussian_luma_ssim"]["value"], 0.715144741)
        self.assertEqual(pavilion_metrics["regional_ssim_p5"]["value"], 0.471018181)
        self.assertEqual(
            pavilion_metrics["sobel_gradient_energy_ratio"]["value"], 1.7515811
        )

        with Image.open(koro / "draft.png") as image:
            candidate_rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
        with Image.open(koro / "baseline.png") as image:
            reference_rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
        self.assertTrue(legacy_pass(candidate_rgb, reference_rgb))
        koro_result = quality.evaluate_pngs(
            koro / "draft.png",
            koro / "baseline.png",
            target_size=(1080, 1920),
        )
        self.assertFalse(koro_result["pass"])
        self.assertEqual(koro_result["alpha_agreement"]["value"], 0.997718487)
        self.assertEqual(
            koro_result["mattes"]["black"]["metrics"][
                "sobel_gradient_energy_ratio"
            ]["value"],
            2.008684935,
        )


if __name__ == "__main__":
    unittest.main()
