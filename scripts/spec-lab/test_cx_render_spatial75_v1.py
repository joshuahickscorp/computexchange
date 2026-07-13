#!/usr/bin/env python3
"""Parity, artifact-safety, and operator tests for spatial75 v1."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import hashlib
from io import BytesIO
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
from PIL import Image, ImageChops, ImageStat


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cx_render_spatial75_v1 as spatial  # noqa: E402


WIDTH, HEIGHT = spatial.INPUT_SIZE


def synthetic_rgba(seed: int = 9182) -> np.ndarray:
    """Structured, non-premultiplied input with a nontrivial alpha plane."""

    y, x = np.mgrid[0:HEIGHT, 0:WIDTH]
    rng = np.random.default_rng(seed)
    noise = rng.integers(0, 32, (HEIGHT, WIDTH), dtype=np.uint8)
    red = ((x * 17 + y * 3) % 256).astype(np.uint8)
    green = ((x // 3 + y * 13 + noise) % 256).astype(np.uint8)
    blue = (((x ^ y) * 5 + noise) % 256).astype(np.uint8)
    alpha = np.clip((x * 255 // (WIDTH - 1)) + ((y % 19) - 9), 0, 255).astype(
        np.uint8
    )
    # Straight-alpha sentinel: transparent pixels retain saturated RGB.
    red[:16, :16] = 255
    green[:16, :16] = 0
    blue[:16, :16] = 0
    alpha[:16, :16] = 0
    return np.stack((red, green, blue, alpha), axis=2)


def save_png(path: Path, array: np.ndarray, mode: str | None = None) -> None:
    selected = mode or ("RGBA" if array.shape[2] == 4 else "RGB")
    Image.fromarray(array, selected).save(
        path, format="PNG", optimize=False, compress_level=0
    )


def backend_snapshot_claims(path: Path) -> dict[str, object]:
    png = path.read_bytes()
    with Image.open(BytesIO(png)) as image:
        image.load()
        mode = image.mode
        pixels = image.tobytes()
    return {
        "png_bytes": png,
        "png_sha256": hashlib.sha256(png).hexdigest(),
        "png_byte_count": len(png),
        "decoder_mode": mode,
        "decoder_pixel_bytes": pixels,
        "decoder_pixel_sha256": hashlib.sha256(pixels).hexdigest(),
    }


def current_pillow_agreement(
    a_path: Path, b_path: Path
) -> tuple[float, float, list[dict[str, object]], float, int]:
    """Literal copy of the backend's current Pillow/ImageStat algorithm."""

    with Image.open(a_path) as source:
        if source.format != "PNG" or source.size != spatial.INPUT_SIZE:
            raise AssertionError("fixture identity mismatch")
        a = source.convert("RGB")
        a.load()
    with Image.open(b_path) as source:
        if source.format != "PNG" or source.size != spatial.INPUT_SIZE:
            raise AssertionError("fixture identity mismatch")
        b = source.convert("RGB")
        b.load()
    difference = ImageChops.difference(a, b)

    def difference_score(image: Image.Image) -> float:
        means = ImageStat.Stat(image).mean
        value = 1.0 - sum(means) / (len(means) * 255.0)
        return min(1.0, max(0.0, float(value)))

    def grid(columns: int, rows: int) -> list[dict[str, object]]:
        values: list[dict[str, object]] = []
        for row in range(rows):
            top = row * HEIGHT // rows
            bottom = (row + 1) * HEIGHT // rows
            for column in range(columns):
                left = column * WIDTH // columns
                right = (column + 1) * WIDTH // columns
                rect = (left, top, right, bottom)
                values.append(
                    {"rect": rect, "score": difference_score(difference.crop(rect))}
                )
        return values

    columns, rows = spatial._agreement_grid(spatial.INPUT_SIZE)
    tiles = grid(columns, rows)
    micro_columns, micro_rows = spatial._microtile_grid(spatial.INPUT_SIZE)
    microtiles = grid(micro_columns, micro_rows)
    return (
        difference_score(difference),
        min(tile["score"] for tile in tiles),
        tiles,
        min(tile["score"] for tile in microtiles),
        len(microtiles),
    )


class PartitionedAgreementParityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_gate_exact(self, draft: np.ndarray, verify: np.ndarray) -> spatial.GateResult:
        draft_path = self.root / "draft.png"
        verify_path = self.root / "verify.png"
        save_png(draft_path, draft)
        save_png(verify_path, verify)
        expected = current_pillow_agreement(draft_path, verify_path)
        actual = spatial.gate_pngs(draft_path, verify_path)
        self.assertEqual(actual.legacy_tuple(), expected)
        self.assertFalse(actual.decoded_draft.rgb.flags.writeable)
        self.assertFalse(actual.decoded_draft.alpha.flags.writeable)
        return actual

    def test_random_structured_rgb_is_bit_exact_to_current_pillow_gate(self) -> None:
        draft = synthetic_rgba()[..., :3]
        verify = draft.copy()
        rng = np.random.default_rng(731)
        for _ in range(200):
            left = int(rng.integers(0, WIDTH - 20))
            top = int(rng.integers(0, HEIGHT - 20))
            width = int(rng.integers(1, 20))
            height = int(rng.integers(1, 20))
            verify[top : top + height, left : left + width] = rng.integers(
                0, 256, (height, width, 3), dtype=np.uint8
            )
        result = self.assert_gate_exact(draft, verify)
        self.assertTrue(result.passed)
        self.assertEqual(len(result.tiles), 9 * 16)
        self.assertEqual(result.microtile_count, 25 * 45)

    def test_adversarial_microtile_boundaries_and_channel_extremes_are_exact(self) -> None:
        draft = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
        verify = np.zeros_like(draft)
        micro_columns, micro_rows = spatial._microtile_grid(spatial.INPUT_SIZE)
        # The width is not divisible by 25, so these floor-partition boundaries
        # exercise alternating 32/33-pixel rectangles and all three channels.
        for column in range(1, micro_columns):
            edge = column * WIDTH // micro_columns
            verify[:, edge - 1 : edge + 1, column % 3] = 255
        for row in range(1, micro_rows):
            edge = row * HEIGHT // micro_rows
            verify[edge - 1 : edge + 1, :, row % 3] = 255
        # Put unequal values on both sides of regional and image boundaries.
        verify[0, 0] = (1, 127, 255)
        verify[-1, -1] = (254, 128, 2)
        verify[719:721, 449:451] = (37, 191, 83)
        self.assert_gate_exact(draft, verify)

    def test_rgba_alpha_is_ignored_by_gate_but_retained_for_postprocess(self) -> None:
        draft = synthetic_rgba()
        verify = draft.copy()
        verify[..., 3] = 255 - verify[..., 3]
        result = self.assert_gate_exact(draft, verify)
        self.assertEqual(result.global_score, 1.0)
        self.assertEqual(result.regional_worst, 1.0)
        self.assertEqual(result.microtile_worst, 1.0)
        np.testing.assert_array_equal(result.decoded_draft.alpha, draft[..., 3])

    def test_partition_sums_are_uint64_complete_bounded_and_exact(self) -> None:
        difference = np.full((HEIGHT, WIDTH, 3), 255, dtype=np.uint8)
        for columns, rows in (
            spatial._agreement_grid(spatial.INPUT_SIZE),
            spatial._microtile_grid(spatial.INPUT_SIZE),
        ):
            sums = spatial._partition_channel_sums(difference, columns, rows)
            self.assertEqual(sums.dtype, np.uint64)
            self.assertEqual(sums.shape, (rows, columns, 3))
            self.assertFalse(sums.flags.writeable)
            for index, rect in enumerate(
                spatial._rectangles(spatial.INPUT_SIZE, columns, rows)
            ):
                left, top, right, bottom = rect
                row, column = divmod(index, columns)
                expected = (right - left) * (bottom - top) * 255
                np.testing.assert_array_equal(
                    sums[row, column], np.full(3, expected, dtype=np.uint64)
                )
                self.assertLess(expected, 2**64)
            np.testing.assert_array_equal(
                np.sum(sums, axis=(0, 1), dtype=np.uint64),
                np.full(3, WIDTH * HEIGHT * 255, dtype=np.uint64),
            )


class StrictInputAndPublicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.rgba = synthetic_rgba(101)
        self.input = self.root / "input.png"
        save_png(self.input, self.rgba)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_malformed_signature_crc_dimensions_palette_and_depth_reject(self) -> None:
        invalid = self.root / "signature.png"
        invalid.write_bytes(b"not a png")
        with self.assertRaisesRegex(spatial.Spatial75Error, "invalid_png_signature"):
            spatial.decode_png(invalid)

        crc = self.root / "crc.png"
        damaged = bytearray(self.input.read_bytes())
        idat = damaged.index(b"IDAT")
        damaged[idat + 4] ^= 1
        crc.write_bytes(damaged)
        with self.assertRaisesRegex(spatial.Spatial75Error, "png_crc_mismatch"):
            spatial.decode_png(crc)

        dimensions = self.root / "dimensions.png"
        Image.new("RGB", (WIDTH - 1, HEIGHT), (1, 2, 3)).save(dimensions)
        with self.assertRaisesRegex(spatial.Spatial75Error, "png_dimension_mismatch"):
            spatial.decode_png(dimensions)

        palette = self.root / "palette.png"
        # Index 255 forces an 8-bit palette IHDR so the color-type rejection is
        # tested independently from the bit-depth rejection below.
        Image.new("P", spatial.INPUT_SIZE, 255).save(palette, bits=8)
        with self.assertRaisesRegex(
            spatial.Spatial75Error, "png_color_type_not_rgb_or_rgba"
        ):
            spatial.decode_png(palette)

        depth = self.root / "depth.png"
        Image.fromarray(np.zeros((HEIGHT, WIDTH), dtype=np.uint16)).save(depth)
        with self.assertRaisesRegex(
            spatial.Spatial75Error, "png_bit_depth_not_8"
        ):
            spatial.decode_png(depth)

    def test_rgb_and_alpha_bicubic_are_exact_and_straight(self) -> None:
        output = self.root / "output.png"
        original_decoder = spatial._pillow_decode_validated_png
        with mock.patch.object(
            spatial,
            "_pillow_decode_validated_png",
            wraps=original_decoder,
        ) as decoder:
            result = spatial.postprocess_png(self.input, output)
        self.assertTrue(
            any(call.kwargs.get("capture_pixels") is False for call in decoder.mock_calls),
            "encoded output must retain a full Pillow decoder validation",
        )
        with Image.open(output) as actual:
            self.assertEqual(actual.format, "PNG")
            self.assertEqual(actual.mode, "RGBA")
            self.assertEqual(actual.size, spatial.OUTPUT_SIZE)
            actual.load()
            actual_copy = actual.copy()

        source_rgb = Image.fromarray(self.rgba[..., :3], "RGB").resize(
            spatial.OUTPUT_SIZE, Image.Resampling.BICUBIC
        )
        source_alpha = Image.fromarray(self.rgba[..., 3], "L").resize(
            spatial.OUTPUT_SIZE, Image.Resampling.BICUBIC
        )
        expected = Image.merge("RGBA", (*source_rgb.split(), source_alpha))
        self.assertIsNone(ImageChops.difference(actual_copy, expected).getbbox())
        # Transparent saturated red was resized in straight RGB, independently
        # from alpha; it must not collapse to black via premultiplication.
        self.assertGreater(actual_copy.getpixel((0, 0))[0], 240)
        self.assertEqual(actual_copy.getpixel((0, 0))[3], 0)
        self.assertEqual(result.input_sha256, spatial._sha256_file(self.input))
        self.assertEqual(result.output_sha256, spatial._sha256_file(output))
        self.assertFalse(result.input_decode_reused)
        self.assertEqual(
            set(result.timings_ns),
            {"read", "decode", "transform", "encode", "validate", "publish", "total"},
        )
        self.assertTrue(all(value >= 0 for value in result.timings_ns.values()))
        _mode, compressed = spatial._validate_png_container(
            output.read_bytes(), spatial.OUTPUT_SIZE, required_mode="RGBA"
        )
        self.assertEqual(compressed[:2], b"\x78\x01")
        self.assertEqual((compressed[2] >> 1) & 0b11, 0)

    def test_output_is_atomic_no_clobber_and_existing_bytes_survive(self) -> None:
        output = self.root / "occupied.png"
        output.write_bytes(b"owner data")
        with self.assertRaisesRegex(spatial.Spatial75Error, "output_exists"):
            spatial.postprocess_png(self.input, output)
        self.assertEqual(output.read_bytes(), b"owner data")
        self.assertEqual(list(self.root.glob(f".{output.name}.*.tmp")), [])

    def test_publication_rejects_stage_path_substitution(self) -> None:
        output = self.root / "substituted.png"
        payload = b"expected-published-bytes"
        real_link = spatial.os.link

        def substitute(source, destination, *, follow_symlinks=True):
            source_path = Path(source)
            stolen = source_path.with_name(source_path.name + ".stolen")
            source_path.rename(stolen)
            source_path.write_bytes(b"x" * len(payload))
            return real_link(
                source_path,
                destination,
                follow_symlinks=follow_symlinks,
            )

        with mock.patch.object(spatial.os, "link", side_effect=substitute):
            with self.assertRaisesRegex(
                spatial.Spatial75Error, "publication_identity"
            ):
                spatial._publish_new(output, payload)
        self.assertFalse(output.exists())

    def test_output_bytes_and_runtime_pins_are_deterministic(self) -> None:
        decoded, _timings = spatial.decode_png(self.input)
        first = spatial.postprocess_decoded(decoded, self.root / "first.png")
        second = spatial.postprocess_decoded(decoded, self.root / "second.png")
        self.assertEqual(first.output_sha256, second.output_sha256)
        self.assertEqual(
            (self.root / "first.png").read_bytes(),
            (self.root / "second.png").read_bytes(),
        )
        identity = spatial.runtime_identity()
        self.assertEqual(identity["versions"]["pillow"], spatial.PIL.__version__)
        self.assertEqual(identity["versions"]["numpy"], spatial.np.__version__)
        self.assertEqual(
            identity["operators"],
            {
                "PIL.Image.Resampling.BICUBIC": 3,
                "expected_bicubic_enum": 3,
            },
        )
        for record in identity["compiled_modules"].values():
            self.assertIsNotNone(record["file"])
            self.assertRegex(record["sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(identity["dependency_tree_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(identity["module_sha256"], r"^[0-9a-f]{64}$")

    def test_rgb_input_receives_separately_resized_opaque_alpha(self) -> None:
        rgb_path = self.root / "rgb.png"
        output = self.root / "rgb-output.png"
        save_png(rgb_path, self.rgba[..., :3])
        spatial.postprocess_png(rgb_path, output)
        with Image.open(output) as image:
            self.assertEqual(image.mode, "RGBA")
            alpha = np.asarray(image.getchannel("A"), dtype=np.uint8)
        self.assertEqual(int(alpha.min()), 255)
        self.assertEqual(int(alpha.max()), 255)


class EndToEndHelperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.draft_array = synthetic_rgba(412)
        self.verify_array = self.draft_array.copy()
        self.verify_array[300:305, 400:410, :3] ^= np.uint8(3)
        self.draft = self.root / "draft.png"
        self.verify = self.root / "verify.png"
        save_png(self.draft, self.draft_array)
        save_png(self.verify, self.verify_array)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_helper_reuses_selected_draft_decode_and_times_full_path(self) -> None:
        output = self.root / "selected.png"
        original = spatial._decode_png_bytes
        decoded_hashes: list[str] = []

        def recording_decode(data: bytes, size: tuple[int, int]) -> spatial.DecodedPng:
            decoded_hashes.append(spatial._sha256_bytes(data))
            return original(data, size)

        with mock.patch.object(spatial, "_decode_png_bytes", side_effect=recording_decode):
            result = spatial.benchmark_gate_and_postprocess(
                self.draft, self.verify, output
            )
        draft_hash = spatial._sha256_file(self.draft)
        self.assertEqual(decoded_hashes.count(draft_hash), 1)
        self.assertTrue(result.gate.passed)
        self.assertTrue(result.postprocess.input_decode_reused)
        self.assertEqual(result.postprocess.timings_ns["decode"], 0)
        self.assertEqual(
            set(result.timings_ns),
            {
                "read",
                "decode",
                "gate_difference",
                "gate_partition",
                "gate_score",
                "transform",
                "encode",
                "validate",
                "publish",
                "gate",
                "selected_draft_postprocess",
                "total",
            },
        )
        self.assertGreaterEqual(
            result.timings_ns["total"], result.timings_ns["gate"]
        )
        receipt = result.receipt()
        self.assertFalse(receipt["policy"]["quality_claim"])
        self.assertTrue(receipt["policy"]["experimental"])
        self.assertRegex(receipt["policy_sha256"], r"^[0-9a-f]{64}$")

    def test_rejected_gate_does_not_publish(self) -> None:
        black = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
        white = np.full_like(black, 255)
        save_png(self.draft, black)
        save_png(self.verify, white)
        output = self.root / "must-not-exist.png"
        with self.assertRaises(spatial.GateRejected):
            spatial.benchmark_gate_and_postprocess(self.draft, self.verify, output)
        self.assertFalse(output.exists())

    def test_cli_emits_receipt_and_is_no_clobber(self) -> None:
        output = self.root / "cli.png"
        receipt = self.root / "receipt.json"
        command = [
            sys.executable,
            str(HERE / "cx_render_spatial75_v1.py"),
            "benchmark",
            str(self.draft),
            str(self.verify),
            str(output),
            "--receipt",
            str(receipt),
        ]
        first = subprocess.run(command, text=True, capture_output=True, check=False)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(output.is_file())
        self.assertTrue(receipt.is_file())
        second = subprocess.run(command, text=True, capture_output=True, check=False)
        self.assertEqual(second.returncode, 2)
        self.assertIn('"error": "output_exists"', second.stderr)


class PipelinedApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.draft_array = synthetic_rgba(867)
        self.verify_array = self.draft_array.copy()
        self.verify_array[250:258, 310:322, :3] ^= np.uint8(5)
        self.draft_path = self.root / "draft.png"
        self.verify_path = self.root / "verify.png"
        save_png(self.draft_path, self.draft_array)
        save_png(self.verify_path, self.verify_array)
        self.decoded, _timings = spatial.decode_png(self.draft_path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def prepare_and_gate(
        self,
    ) -> tuple[spatial.PreparedOutput, spatial.GateResult]:
        # Both workers read the same non-writeable decoded planes. In product,
        # preparation starts while the independent verify render is on the GPU;
        # the gate begins as soon as verify.png becomes available.
        with ThreadPoolExecutor(max_workers=2) as executor:
            prepare_future = executor.submit(
                spatial.prepare_decoded_draft, self.decoded
            )
            gate_future = executor.submit(
                spatial.gate_decoded_draft, self.decoded, self.verify_path
            )
            return prepare_future.result(), gate_future.result()

    def test_concurrent_prepare_gate_then_publish_is_byte_exact_to_sequential(self) -> None:
        prepared, gate = self.prepare_and_gate()
        self.assertTrue(gate.passed)
        self.assertEqual(gate.timings_ns["read_draft"], 0)
        self.assertEqual(gate.timings_ns["decode_draft"], 0)
        self.assertFalse((self.root / "pipeline.png").exists())

        pipeline = spatial.publish_prepared_after_gate(
            prepared, gate, self.root / "pipeline.png"
        )
        sequential = spatial.postprocess_decoded(
            self.decoded, self.root / "sequential.png"
        )
        self.assertEqual(pipeline.output_sha256, sequential.output_sha256)
        self.assertEqual(pipeline.output_bytes, sequential.output_bytes)
        self.assertEqual(
            (self.root / "pipeline.png").read_bytes(),
            (self.root / "sequential.png").read_bytes(),
        )
        self.assertEqual(prepared.output_sha256, pipeline.output_sha256)
        self.assertEqual(
            prepared.encoded_png, (self.root / "pipeline.png").read_bytes()
        )
        self.assertIn("publication_call", pipeline.timings_ns)
        self.assertGreaterEqual(
            pipeline.timings_ns["total"], prepared.timings_ns.total
        )

        independently_decoded_gate = spatial.gate_pngs(
            self.draft_path, self.verify_path
        )
        self.assertEqual(gate.legacy_tuple(), independently_decoded_gate.legacy_tuple())
        receipt = prepared.receipt()
        self.assertFalse(receipt["publication_authorized"])
        self.assertRegex(receipt["binding_sha256"], r"^[0-9a-f]{64}$")

    def test_rejected_gate_never_publishes_prepared_bytes(self) -> None:
        rejected_verify = self.root / "rejected-verify.png"
        save_png(
            rejected_verify,
            np.full((HEIGHT, WIDTH, 3), 255, dtype=np.uint8),
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            prepare_future = executor.submit(
                spatial.prepare_decoded_draft, self.decoded
            )
            gate_future = executor.submit(
                spatial.gate_decoded_draft, self.decoded, rejected_verify
            )
            prepared = prepare_future.result()
            gate = gate_future.result()
        self.assertFalse(gate.passed)
        output = self.root / "rejected-output.png"
        with self.assertRaises(spatial.GateRejected):
            spatial.publish_prepared_after_gate(prepared, gate, output)
        self.assertFalse(output.exists())

    def test_prepared_and_gate_draft_identity_mismatch_fails_closed(self) -> None:
        prepared = spatial.prepare_decoded_draft(self.decoded)
        other_array = self.draft_array.copy()
        other_array[100:132, 200:236, :3] ^= np.uint8(31)
        other_path = self.root / "other.png"
        save_png(other_path, other_array)
        other_decoded, _ = spatial.decode_png(other_path)
        # Identity comparison passes, but it authorizes the other draft only.
        gate = spatial.gate_decoded_draft(other_decoded, other_path)
        self.assertTrue(gate.passed)
        output = self.root / "identity-mismatch.png"
        with self.assertRaisesRegex(
            spatial.Spatial75Error, "prepared_gate_draft_identity_mismatch"
        ):
            spatial.publish_prepared_after_gate(prepared, gate, output)
        self.assertFalse(output.exists())

    def test_prepared_bytes_are_immutable_and_tampering_fails_before_publish(self) -> None:
        prepared, gate = self.prepare_and_gate()
        self.assertIs(type(prepared.encoded_png), bytes)
        with self.assertRaises(FrozenInstanceError):
            prepared.output_sha256 = "0" * 64  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            prepared.timings_ns.total = 0  # type: ignore[misc]

        corrupted_bytes = bytearray(prepared.encoded_png)
        corrupted_bytes[-20] ^= 1
        corrupted = replace(prepared, encoded_png=bytes(corrupted_bytes))
        output = self.root / "corrupted.png"
        with self.assertRaisesRegex(
            spatial.Spatial75Error, "prepared_output_identity_mismatch"
        ):
            spatial.publish_prepared_after_gate(corrupted, gate, output)
        self.assertFalse(output.exists())

    def test_runtime_or_mutable_draft_change_fails_closed(self) -> None:
        prepared, gate = self.prepare_and_gate()
        changed_runtime = dict(spatial.runtime_identity())
        changed_runtime["dependency_tree_sha256"] = "0" * 64
        output = self.root / "runtime-mismatch.png"
        with mock.patch.object(spatial, "runtime_identity", return_value=changed_runtime):
            with self.assertRaisesRegex(
                spatial.Spatial75Error, "gate_result_identity_mismatch"
            ):
                spatial.publish_prepared_after_gate(prepared, gate, output)
        self.assertFalse(output.exists())

        changed_decoded, _ = spatial.decode_png(self.draft_path)
        changed_decoded.rgb.setflags(write=True)
        with self.assertRaisesRegex(
            spatial.Spatial75Error, "decoded_input_identity_mismatch"
        ):
            spatial.prepare_decoded_draft(changed_decoded)

    def test_fused_gate_publish_is_exact_and_avoids_duplicate_large_hashes(self) -> None:
        prepared = spatial.prepare_decoded_draft(self.decoded)
        verify, _ = spatial.decode_png(self.verify_path)
        output = self.root / "fused.png"
        identity_calls = 0
        hashed_lengths: list[int] = []
        original_identity = spatial._decoded_identity_sha256
        original_hash = spatial._sha256_bytes

        def record_identity(decoded: spatial.DecodedPng) -> str:
            nonlocal identity_calls
            identity_calls += 1
            return original_identity(decoded)

        def record_hash(value: bytes) -> str:
            hashed_lengths.append(len(value))
            return original_hash(value)

        with mock.patch.object(
            spatial, "_decoded_identity_sha256", side_effect=record_identity
        ), mock.patch.object(spatial, "_sha256_bytes", side_effect=record_hash), mock.patch.object(
            spatial,
            "_validate_prepared_output",
            side_effect=AssertionError("fused path must use the immutable seal"),
        ), mock.patch.object(
            spatial,
            "_validate_gate_result",
            side_effect=AssertionError("locally built gate must not be revalidated"),
        ):
            fused = spatial.gate_decoded_pair_and_publish_prepared(
                prepared, self.decoded, verify, output
            )
        self.assertTrue(fused.gate.passed)
        self.assertEqual(identity_calls, 2)
        self.assertNotIn(prepared.output_bytes, hashed_lengths)
        self.assertEqual(output.read_bytes(), prepared.encoded_png)
        self.assertEqual(fused.postprocess.output_sha256, prepared.output_sha256)
        ordinary = spatial.gate_decoded_pair(self.decoded, verify)
        self.assertEqual(fused.gate.legacy_tuple(), ordinary.legacy_tuple())

    def test_fused_rejection_and_substitution_never_publish(self) -> None:
        prepared = spatial.prepare_decoded_draft(self.decoded)
        rejected_path = self.root / "fused-rejected-verify.png"
        save_png(
            rejected_path,
            np.full((HEIGHT, WIDTH, 3), 255, dtype=np.uint8),
        )
        rejected, _ = spatial.decode_png(rejected_path)
        rejected_output = self.root / "fused-rejected.png"
        with self.assertRaises(spatial.GateRejected):
            spatial.gate_decoded_pair_and_publish_prepared(
                prepared, self.decoded, rejected, rejected_output
            )
        self.assertFalse(rejected_output.exists())

        verify, _ = spatial.decode_png(self.verify_path)
        corrupted_bytes = bytearray(prepared.encoded_png)
        corrupted_bytes[-30] ^= 1
        corrupted = replace(prepared, encoded_png=bytes(corrupted_bytes))
        corrupted_output = self.root / "fused-corrupted.png"
        with self.assertRaisesRegex(
            spatial.Spatial75Error, "prepared_output_identity_mismatch"
        ):
            spatial.gate_decoded_pair_and_publish_prepared(
                corrupted, self.decoded, verify, corrupted_output
            )
        self.assertFalse(corrupted_output.exists())

        other_array = self.draft_array.copy()
        other_array[20:28, 30:42, :3] ^= np.uint8(17)
        other_path = self.root / "fused-other-draft.png"
        save_png(other_path, other_array)
        other, _ = spatial.decode_png(other_path)
        mismatch_output = self.root / "fused-mismatch.png"
        with self.assertRaisesRegex(
            spatial.Spatial75Error, "prepared_gate_draft_identity_mismatch"
        ):
            spatial.gate_decoded_pair_and_publish_prepared(
                prepared, other, other, mismatch_output
            )
        self.assertFalse(mismatch_output.exists())


class RetainedBackendHandoffTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.draft_array = synthetic_rgba(1771)
        self.verify_array = self.draft_array.copy()
        self.verify_array[77:86, 331:347, :3] ^= np.uint8(7)
        self.draft_path = self.root / "draft.png"
        self.verify_path = self.root / "verify.png"
        save_png(self.draft_path, self.draft_array)
        save_png(self.verify_path, self.verify_array)
        self.draft_claims = backend_snapshot_claims(self.draft_path)
        self.verify_claims = backend_snapshot_claims(self.verify_path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def snapshot(self, claims: dict[str, object]) -> spatial.DecodedPng:
        return spatial.decoded_png_from_backend_snapshot(**claims)  # type: ignore[arg-type]

    def test_handoff_runs_strict_validation_without_second_pillow_decode(self) -> None:
        with mock.patch.object(
            spatial,
            "_pillow_decode_validated_png",
            side_effect=AssertionError("handoff must not run Pillow"),
        ):
            decoded = self.snapshot(self.draft_claims)
        np.testing.assert_array_equal(decoded.rgb, self.draft_array[..., :3])
        np.testing.assert_array_equal(decoded.alpha, self.draft_array[..., 3])
        self.assertFalse(decoded.rgb.flags.writeable)
        self.assertFalse(decoded.alpha.flags.writeable)
        with self.assertRaises(ValueError):
            decoded.rgb.setflags(write=True)
        with self.assertRaises(ValueError):
            decoded.alpha.setflags(write=True)

    def test_decoded_pair_gate_and_prepared_output_are_exact(self) -> None:
        draft = self.snapshot(self.draft_claims)
        verify = self.snapshot(self.verify_claims)
        retained_gate = spatial.gate_decoded_pair(draft, verify)
        path_gate = spatial.gate_pngs(self.draft_path, self.verify_path)
        self.assertEqual(retained_gate.legacy_tuple(), path_gate.legacy_tuple())
        for key in ("read_draft", "read_verify", "decode_draft", "decode_verify"):
            self.assertEqual(retained_gate.timings_ns[key], 0)

        prepared = spatial.prepare_decoded_draft(draft)
        retained_output = self.root / "retained.png"
        result = spatial.publish_prepared_after_gate(
            prepared, retained_gate, retained_output
        )
        sequential_output = self.root / "sequential.png"
        sequential = spatial.postprocess_png(self.draft_path, sequential_output)
        self.assertEqual(result.output_sha256, sequential.output_sha256)
        self.assertEqual(retained_output.read_bytes(), sequential_output.read_bytes())

    def test_data_pixel_mode_sha_and_count_substitution_reject(self) -> None:
        valid = dict(self.draft_claims)
        other_png = self.verify_path.read_bytes()
        cases: list[tuple[str, dict[str, object]]] = []

        data_substitution = dict(valid)
        data_substitution["png_bytes"] = other_png
        data_substitution["png_byte_count"] = len(other_png)
        cases.append(("data", data_substitution))

        pixel_substitution = dict(valid)
        pixels = bytearray(pixel_substitution["decoder_pixel_bytes"])
        pixels[len(pixels) // 2] ^= 1
        pixel_substitution["decoder_pixel_bytes"] = bytes(pixels)
        cases.append(("pixel", pixel_substitution))

        mode_substitution = dict(valid)
        mode_substitution["decoder_mode"] = "RGB"
        cases.append(("mode", mode_substitution))

        sha_substitution = dict(valid)
        sha_substitution["png_sha256"] = "0" * 64
        cases.append(("sha", sha_substitution))

        count_substitution = dict(valid)
        count_substitution["png_byte_count"] = int(valid["png_byte_count"]) + 1
        cases.append(("count", count_substitution))

        for label, claims in cases:
            with self.subTest(label=label):
                with self.assertRaises(spatial.Spatial75Error):
                    self.snapshot(claims)

    def test_malformed_strict_container_rejects_despite_plausible_pixels(self) -> None:
        claims = dict(self.draft_claims)
        corrupted = bytearray(claims["png_bytes"])
        idat = corrupted.index(b"IDAT")
        corrupted[idat + 4] ^= 1
        claims["png_bytes"] = bytes(corrupted)
        claims["png_sha256"] = hashlib.sha256(claims["png_bytes"]).hexdigest()
        claims["png_byte_count"] = len(claims["png_bytes"])
        with self.assertRaisesRegex(spatial.Spatial75Error, "png_crc_mismatch"):
            self.snapshot(claims)

    def test_wrong_pixel_lengths_and_mutable_buffers_reject(self) -> None:
        shortened = dict(self.draft_claims)
        pixels = shortened["decoder_pixel_bytes"][:-1]
        shortened["decoder_pixel_bytes"] = pixels
        shortened["decoder_pixel_sha256"] = hashlib.sha256(pixels).hexdigest()
        with self.assertRaisesRegex(
            spatial.Spatial75Error, "backend_snapshot_pixel_length_mismatch"
        ):
            self.snapshot(shortened)

        mutable_png = dict(self.draft_claims)
        mutable_png["png_bytes"] = bytearray(mutable_png["png_bytes"])
        with self.assertRaisesRegex(
            spatial.Spatial75Error, "backend_snapshot_png_bytes_invalid"
        ):
            self.snapshot(mutable_png)

        mutable_pixels = dict(self.draft_claims)
        mutable_pixels["decoder_pixel_bytes"] = bytearray(
            mutable_pixels["decoder_pixel_bytes"]
        )
        with self.assertRaisesRegex(
            spatial.Spatial75Error, "backend_snapshot_pixel_bytes_not_immutable"
        ):
            self.snapshot(mutable_pixels)


if __name__ == "__main__":
    unittest.main()
