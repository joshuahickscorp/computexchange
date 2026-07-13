from __future__ import annotations

import copy
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import screen_customer_png_transport as screen  # noqa: E402


class PngTransportScreenTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source.png"
        image = Image.new("RGBA", (32, 24), (20, 40, 80, 255))
        for x in range(16):
            for y in range(12):
                image.putpixel((x, y), (x * 7, y * 9, 100, 128 + x))
        image.save(self.source, "PNG", compress_level=0)

    def test_screen_is_lossless_and_strictly_labeled(self) -> None:
        receipt = screen.run_screen(
            self.source, levels=[0, 1, 3], trials=3, link_rates_mbps=[100]
        )
        self.assertEqual(receipt["kind"], screen.KIND)
        self.assertIn("not exact encoded-byte", receipt["claim_scope"])
        self.assertEqual(len(receipt["rows"]), 3)
        self.assertTrue(all(row["decoded_rgba_exact"] for row in receipt["rows"]))
        self.assertEqual(
            len({row["decoded_rgba_sha256"] for row in receipt["rows"]}), 1
        )

    def test_level_validation_rejects_bool_range_duplicate_and_empty(self) -> None:
        for levels in ([True], [-1], [10], [1, 1], []):
            with self.subTest(levels=levels):
                with self.assertRaises(screen.PngTransportError):
                    screen.validate_levels(levels)

    def test_link_rate_validation_rejects_bad_values(self) -> None:
        for rates in ([0], [-1], [float("nan")], [100, 100], [], [True]):
            with self.subTest(rates=rates):
                with self.assertRaises(screen.PngTransportError):
                    screen.validate_link_rates(rates)

    def test_trial_count_is_odd_and_bounded(self) -> None:
        for count in (0, 2, 4, 33, True):
            with self.subTest(count=count):
                with self.assertRaises(screen.PngTransportError):
                    screen.run_screen(self.source, levels=[0], trials=count)

    def test_non_png_and_symlink_are_rejected(self) -> None:
        text = self.root / "not.png"
        text.write_text("not an image")
        with self.assertRaises(screen.PngTransportError):
            screen.load_png(text)
        link = self.root / "link.png"
        link.symlink_to(self.source)
        with self.assertRaisesRegex(screen.PngTransportError, "non-symlink"):
            screen.load_png(link)

    def test_hard_link_source_is_rejected(self) -> None:
        hard_link = self.root / "hard.png"
        os.link(self.source, hard_link)
        with self.assertRaisesRegex(screen.PngTransportError, "singly-linked"):
            screen.load_png(hard_link)

    def test_type7_matches_hand_values(self) -> None:
        self.assertEqual(screen.type7([1, 2, 3], 0.5), 2)
        self.assertAlmostEqual(screen.type7([1, 2, 3], 0.95), 2.9)
        with self.assertRaises(screen.PngTransportError):
            screen.type7([], 0.5)

    def test_pareto_filter_rejects_strictly_dominated_row(self) -> None:
        rows = [
            {"compress_level": 0, "encoded_bytes": 100, "encode_median_s": 1.0},
            {"compress_level": 1, "encoded_bytes": 80, "encode_median_s": 1.1},
            {"compress_level": 2, "encoded_bytes": 110, "encode_median_s": 1.2},
        ]
        self.assertEqual(screen.pareto_levels(rows), [0, 1])

    def test_no_clobber_output_publication(self) -> None:
        output = self.root / "receipt.json"
        payload = b'{"ok":true}\n'
        screen.publish_no_clobber(output, payload)
        self.assertEqual(output.read_bytes(), payload)
        with self.assertRaisesRegex(screen.PngTransportError, "already exists"):
            screen.publish_no_clobber(output, payload)

    def test_symlink_output_parent_is_rejected(self) -> None:
        real_parent = self.root / "real"
        real_parent.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(real_parent, target_is_directory=True)
        with self.assertRaisesRegex(screen.PngTransportError, "real directory"):
            screen.publish_no_clobber(alias / "receipt.json", b"{}\n")

    def test_encoded_rgba_hash_detects_exact_pixels(self) -> None:
        image, source = screen.load_png(self.source)
        payload, elapsed = screen.encode_png(image, 3)
        self.assertGreater(elapsed, 0)
        self.assertEqual(
            screen.decoded_rgba_sha256(payload), source["decoded_rgba_sha256"]
        )
        parsed = json.loads(screen.canonical_json({"elapsed": elapsed}))
        self.assertGreater(parsed["elapsed"], 0)

    def test_rgb_input_is_normalized_to_exact_rgba(self) -> None:
        rgb = self.root / "rgb.png"
        Image.new("RGB", (8, 8), (1, 2, 3)).save(rgb, "PNG")
        image, record = screen.load_png(rgb)
        self.assertEqual(image.mode, "RGBA")
        payload, _ = screen.encode_png(image, 1)
        self.assertEqual(
            screen.decoded_rgba_sha256(payload), record["decoded_rgba_sha256"]
        )

    def test_palette_png_is_rejected(self) -> None:
        palette = self.root / "palette.png"
        image = Image.new("P", (8, 8))
        output = BytesIO()
        image.save(output, "PNG")
        palette.write_bytes(output.getvalue())
        with self.assertRaisesRegex(screen.PngTransportError, "RGB/RGBA"):
            screen.load_png(palette)

    def test_animated_png_is_rejected_instead_of_collapsed(self) -> None:
        animated = self.root / "animated.png"
        first = Image.new("RGBA", (8, 8), (255, 0, 0, 255))
        second = Image.new("RGBA", (8, 8), (0, 0, 255, 255))
        first.save(
            animated,
            "PNG",
            save_all=True,
            append_images=[second],
            duration=[10, 20],
            loop=0,
        )
        with self.assertRaisesRegex(screen.PngTransportError, "animated PNG"):
            screen.load_png(animated)

    def test_every_measured_payload_is_pixel_verified(self) -> None:
        good_image, _ = screen.load_png(self.source)
        good, _ = screen.encode_png(good_image, 1)
        wrong_image = Image.new("RGBA", good_image.size, (255, 0, 255, 255))
        wrong, _ = screen.encode_png(wrong_image, 1)
        payloads = iter((good, wrong, good, good))

        def forged_encoder(_image: Image.Image, _level: int) -> tuple[bytes, float]:
            return next(payloads), 0.001

        with mock.patch.object(screen, "encode_png", side_effect=forged_encoder):
            with self.assertRaisesRegex(
                screen.PngTransportError, "measured lossless"
            ):
                screen.run_screen(self.source, levels=[1], trials=3)

    def test_measured_encoded_bytes_must_be_deterministic(self) -> None:
        image, _ = screen.load_png(self.source)
        first, _ = screen.encode_png(image, 1)
        second, _ = screen.encode_png(image, 3)
        self.assertNotEqual(first, second)
        payloads = iter((first, first, second, first))

        def unstable_encoder(_image: Image.Image, _level: int) -> tuple[bytes, float]:
            return next(payloads), 0.001

        with mock.patch.object(screen, "encode_png", side_effect=unstable_encoder):
            with self.assertRaisesRegex(screen.PngTransportError, "changed across"):
                screen.run_screen(self.source, levels=[1], trials=3)

    def test_closed_receipt_replays_from_object_bytes_and_path(self) -> None:
        receipt = screen.run_screen(
            self.source,
            levels=[0, 3],
            trials=3,
            link_rates_mbps=[25, 100],
        )
        self.assertEqual(screen.validate_receipt(receipt), receipt)
        raw = screen.canonical_json(receipt, pretty=True)
        self.assertEqual(screen.validate_receipt_bytes(raw), receipt)
        path = self.root / "closed-receipt.json"
        path.write_bytes(raw)
        self.assertEqual(screen.validate_receipt_path(path), receipt)
        self.assertEqual(receipt["receipt_sha256"], screen.receipt_sha256(receipt))
        self.assertEqual(receipt["comparison_baseline_level"], 0)

    def test_closed_receipt_recomputes_speedups_and_line_rate_totals(self) -> None:
        receipt = screen.run_screen(
            self.source,
            levels=[0, 3],
            trials=3,
            link_rates_mbps=[100],
        )
        baseline, candidate = receipt["rows"]
        self.assertEqual(baseline["encode_median_speedup_vs_baseline_x"], 1.0)
        self.assertEqual(baseline["encoded_size_reduction_vs_baseline_x"], 1.0)
        self.assertEqual(
            baseline["link_models"][0][
                "encode_plus_wire_speedup_vs_baseline_x"
            ],
            1.0,
        )
        self.assertEqual(
            candidate["encoded_size_reduction_vs_baseline_x"],
            baseline["encoded_bytes"] / candidate["encoded_bytes"],
        )
        self.assertEqual(screen.validate_receipt(receipt), receipt)

    def test_receipt_self_hash_rejects_unsigned_tampering(self) -> None:
        receipt = screen.run_screen(
            self.source, levels=[0, 3], trials=3, link_rates_mbps=[100]
        )
        receipt["rows"][0]["encode_median_s"] += 1.0
        with self.assertRaisesRegex(screen.PngTransportError, "self SHA"):
            screen.validate_receipt(receipt, verify_external=False)

    def test_resigned_semantic_tampering_is_rejected(self) -> None:
        original = screen.run_screen(
            self.source, levels=[0, 3], trials=3, link_rates_mbps=[25, 100]
        )

        def resign(value: dict) -> dict:
            value["receipt_sha256"] = screen.receipt_sha256(value)
            return value

        mutations = {
            "median": lambda value: value["rows"][0].__setitem__(
                "encode_median_s", value["rows"][0]["encode_median_s"] + 1.0
            ),
            "p95": lambda value: value["rows"][0].__setitem__(
                "encode_p95_s_type7", value["rows"][0]["encode_p95_s_type7"] + 1.0
            ),
            "wire": lambda value: value["rows"][0]["link_models"][0].__setitem__(
                "wire_floor_s", value["rows"][0]["link_models"][0]["wire_floor_s"] + 1.0
            ),
            "total": lambda value: value["rows"][0]["link_models"][0].__setitem__(
                "encode_plus_wire_s",
                value["rows"][0]["link_models"][0]["encode_plus_wire_s"] + 1.0,
            ),
            "speedup": lambda value: value["rows"][1].__setitem__(
                "encode_median_speedup_vs_baseline_x", 999.0
            ),
            "link_speedup": lambda value: value["rows"][1]["link_models"][0].__setitem__(
                "encode_plus_wire_speedup_vs_baseline_x", 999.0
            ),
            "pareto": lambda value: value.__setitem__("pareto_compression_levels", []),
            "trial_identity": lambda value: value["rows"][0][
                "encoded_trial_identities"
            ][0].__setitem__("sha256", "0" * 64),
            "decoded_identity": lambda value: value["rows"][0].__setitem__(
                "decoded_rgba_sha256", "0" * 64
            ),
            "environment": lambda value: value["environment"].__setitem__(
                "pillow_version", "forged"
            ),
            "code_pin": lambda value: value["pins"]["screen_module"].__setitem__(
                "sha256", "0" * 64
            ),
            "unknown": lambda value: value.__setitem__("unknown", True),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                tampered = copy.deepcopy(original)
                mutate(tampered)
                resign(tampered)
                with self.assertRaises(screen.PngTransportError):
                    screen.validate_receipt(tampered, verify_external=False)

    def test_external_replay_rejects_forged_candidate_identity(self) -> None:
        receipt = screen.run_screen(
            self.source, levels=[1], trials=3, link_rates_mbps=[100]
        )
        forged = copy.deepcopy(receipt)
        forged["rows"][0]["encoded_sha256"] = "0" * 64
        for identity in forged["rows"][0]["encoded_trial_identities"]:
            identity["sha256"] = "0" * 64
        forged["receipt_sha256"] = screen.receipt_sha256(forged)
        self.assertEqual(
            screen.validate_receipt(forged, verify_external=False), forged
        )
        with self.assertRaisesRegex(screen.PngTransportError, "candidate identity"):
            screen.validate_receipt(forged, verify_external=True)

    def test_external_replay_rejects_changed_source(self) -> None:
        receipt = screen.run_screen(
            self.source, levels=[1], trials=3, link_rates_mbps=[100]
        )
        Image.new("RGBA", (32, 24), (255, 0, 0, 255)).save(self.source, "PNG")
        with self.assertRaisesRegex(screen.PngTransportError, "source identity"):
            screen.validate_receipt(receipt, verify_external=True)

    def test_strict_receipt_ingress_rejects_duplicate_nonfinite_and_bounds(self) -> None:
        receipt = screen.run_screen(
            self.source, levels=[1], trials=3, link_rates_mbps=[100]
        )
        raw = screen.canonical_json(receipt)
        duplicate = raw.replace(
            b'{"claim_scope":', b'{"kind":"duplicate","claim_scope":', 1
        )
        with self.assertRaisesRegex(screen.PngTransportError, "duplicate"):
            screen.parse_receipt(duplicate)
        with self.assertRaisesRegex(screen.PngTransportError, "constant"):
            screen.parse_receipt(b'{"sample":NaN}')
        with mock.patch.object(screen, "MAX_RECEIPT_BYTES", 8):
            with self.assertRaisesRegex(screen.PngTransportError, "1..8"):
                screen.parse_receipt(raw)

    def test_receipt_path_rejects_symlink_and_hard_link(self) -> None:
        receipt = screen.run_screen(
            self.source, levels=[1], trials=3, link_rates_mbps=[100]
        )
        path = self.root / "receipt.json"
        path.write_bytes(screen.canonical_json(receipt))
        symlink = self.root / "receipt-link.json"
        symlink.symlink_to(path)
        with self.assertRaisesRegex(screen.PngTransportError, "non-symlink"):
            screen.validate_receipt_path(symlink)
        hard_link = self.root / "receipt-hard.json"
        os.link(path, hard_link)
        with self.assertRaisesRegex(screen.PngTransportError, "identity changed"):
            screen.validate_receipt_path(path)


if __name__ == "__main__":
    unittest.main()
