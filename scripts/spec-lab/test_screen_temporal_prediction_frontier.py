#!/usr/bin/env python3
"""Pure CPU tests for the temporal prediction frontier helpers."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import screen_temporal_prediction_frontier as frontier  # noqa: E402


V3_RECEIPT = Path(
    "/Users/scammermike/.cache/cx-spec-lab/frontier/"
    "koro-temporal-prediction-f11-20260713-closed-v3/"
    "temporal-prediction-frontier.json"
)


class TemporalPredictionFrontierTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_linear_rgba_extrapolation_clips_both_bounds(self) -> None:
        frame9 = np.array(
            [[
                [200, 10, 60, 255],
                [10, 200, 60, 128],
                [10, 20, 30, 40],
            ]],
            dtype=np.uint8,
        )
        frame10 = np.array(
            [[
                [10, 200, 60, 255],
                [200, 10, 60, 64],
                [11, 22, 33, 44],
            ]],
            dtype=np.uint8,
        )
        with (
            mock.patch.object(frontier, "HEIGHT", 1),
            mock.patch.object(frontier, "WIDTH", 3),
        ):
            actual = frontier.linear_extrapolation(frame9, frame10)
        expected = np.array(
            [[
                [0, 255, 60, 255],
                [255, 0, 60, 0],
                [12, 24, 36, 48],
            ]],
            dtype=np.uint8,
        )
        np.testing.assert_array_equal(actual, expected)
        self.assertTrue(actual.flags.c_contiguous)

    def test_direct_prior_returns_an_independent_eager_copy(self) -> None:
        prior = np.arange(24, dtype=np.uint8).reshape(2, 3, 4)
        predicted = frontier.direct_prior(np.zeros_like(prior), prior)
        np.testing.assert_array_equal(predicted, prior)
        self.assertFalse(np.shares_memory(predicted, prior))
        self.assertTrue(predicted.flags.writeable)

    def test_type7_summary_reports_median_p95_and_slowest(self) -> None:
        summary = frontier.summarize([float(value) for value in range(1, 10)])
        self.assertEqual(summary["count"], 9)
        self.assertEqual(summary["median_s"], 5.0)
        self.assertAlmostEqual(summary["p95_s_type7"], 8.6)
        self.assertEqual(summary["slowest_s"], 9.0)
        self.assertEqual(summary["minimum_s"], 1.0)

    def test_reconstruction_floor_stops_after_three_when_cutoff_is_lost(self) -> None:
        frame = np.zeros((1, 1, 4), dtype=np.uint8)
        with (
            mock.patch.object(frontier, "HEIGHT", 1),
            mock.patch.object(frontier, "WIDTH", 1),
            mock.patch.object(frontier, "TRIALS", 9),
            mock.patch.object(frontier, "CUTOFF_1000X_SECONDS", 0.0),
        ):
            result = frontier.reconstruction_floor(
                frontier.direct_prior, frame, frame
            )
        self.assertTrue(result["early_stopped"])
        self.assertEqual(result["count"], 3)
        self.assertEqual(len(result["samples_s"]), 3)

    def test_durable_publication_is_no_clobber(self) -> None:
        destination = self.root / "proof.json"
        payload = b'{"pass":true}\n'
        elapsed_ns = frontier.publish_new(destination, payload)
        self.assertGreater(elapsed_ns, 0)
        self.assertEqual(json.loads(destination.read_text()), {"pass": True})
        with self.assertRaisesRegex(frontier.FrontierError, "no-clobber"):
            frontier.publish_new(destination, b"replacement")

    def test_publication_rejects_stage_path_substitution(self) -> None:
        destination = self.root / "proof.json"
        payload = b'{"pass":true}\n'
        real_link = os.link

        def substitute_then_link(
            source: str,
            target: str,
            *,
            src_dir_fd: int,
            dst_dir_fd: int,
            follow_symlinks: bool,
        ) -> None:
            self.assertEqual(src_dir_fd, dst_dir_fd)
            self.assertFalse(follow_symlinks)
            os.unlink(source, dir_fd=src_dir_fd)
            replacement_fd = os.open(
                source,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=src_dir_fd,
            )
            try:
                os.write(replacement_fd, b"x" * len(payload))
                os.fsync(replacement_fd)
            finally:
                os.close(replacement_fd)
            real_link(
                source,
                target,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
                follow_symlinks=follow_symlinks,
            )

        with (
            mock.patch.object(frontier.os, "link", side_effect=substitute_then_link),
            self.assertRaisesRegex(frontier.FrontierError, "identity changed"),
        ):
            frontier.publish_new(destination, payload)

        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_publication_preserves_destination_created_during_link(self) -> None:
        destination = self.root / "proof.json"
        payload = b'{"pass":true}\n'
        competing_payload = b'{"owner":"other"}\n'
        real_link = os.link

        def create_destination_then_link(
            source: str,
            target: str,
            *,
            src_dir_fd: int,
            dst_dir_fd: int,
            follow_symlinks: bool,
        ) -> None:
            competing_fd = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=dst_dir_fd,
            )
            try:
                os.write(competing_fd, competing_payload)
                os.fsync(competing_fd)
            finally:
                os.close(competing_fd)
            real_link(
                source,
                target,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
                follow_symlinks=follow_symlinks,
            )

        with (
            mock.patch.object(
                frontier.os,
                "link",
                side_effect=create_destination_then_link,
            ),
            self.assertRaisesRegex(frontier.FrontierError, "no-clobber"),
        ):
            frontier.publish_new(destination, payload)

        self.assertEqual(destination.read_bytes(), competing_payload)
        self.assertEqual(list(self.root.iterdir()), [destination])

    def test_source_pin_rejects_a_different_digest(self) -> None:
        expected = frontier.PINNED_INPUTS[9]
        common = {
            "frame": 9,
            "path": self.root / "frame9.png",
            "source_bytes": expected["bytes"],
            "rgba": np.zeros((0,), dtype=np.uint8),
            "rgba_sha256": "0" * 64,
            "timings_ns": {},
        }
        frontier.assert_pinned_frame(
            frontier.DecodedFrame(source_sha256=expected["sha256"], **common)
        )
        with self.assertRaisesRegex(frontier.FrontierError, "immutable pin"):
            frontier.assert_pinned_frame(
                frontier.DecodedFrame(source_sha256="f" * 64, **common)
            )

    def _v3_receipt(self) -> dict:
        if not V3_RECEIPT.is_file():
            self.skipTest("fresh closed-v3 temporal receipt is not present")
        return frontier._strict_json(V3_RECEIPT.read_bytes(), V3_RECEIPT.name)

    def test_strict_validator_accepts_closed_v3_receipt(self) -> None:
        self._v3_receipt()
        receipt = frontier.validate_receipt_path(V3_RECEIPT)
        self.assertEqual(receipt["schema_version"], frontier.SCHEMA_VERSION)

    def test_strict_validator_rejects_stale_screen_pin(self) -> None:
        receipt = self._v3_receipt()
        receipt["code_pins"]["screen_module"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(frontier.FrontierError, "code pin"):
            frontier.validate_receipt(receipt, output_root=V3_RECEIPT.parent)

    def test_strict_validator_rejects_forged_baseline_provenance(self) -> None:
        receipt = self._v3_receipt()
        receipt["baseline_provenance"]["trial_count"] = 9
        with self.assertRaisesRegex(frontier.FrontierError, "baseline provenance"):
            frontier.validate_receipt(receipt, output_root=V3_RECEIPT.parent)

    def test_strict_validator_rejects_forged_composed_speed(self) -> None:
        receipt = self._v3_receipt()
        receipt["arms"][0]["speed"]["composed_charged_estimate"][
            "speedup_x"
        ] = 10_000.0
        with self.assertRaisesRegex(frontier.FrontierError, "speed arithmetic"):
            frontier.validate_receipt(receipt, output_root=V3_RECEIPT.parent)

    def test_strict_validator_rejects_omitted_timing_component(self) -> None:
        receipt = self._v3_receipt()
        del receipt["arms"][0]["trials"][0]["timings_ns"]["png0_encode"]
        with self.assertRaisesRegex(frontier.FrontierError, "timing shape"):
            frontier.validate_receipt(receipt, output_root=V3_RECEIPT.parent)

    def test_artifact_resolver_rejects_hash_forgery_and_path_escape(self) -> None:
        artifact = self.root / "evidence.json"
        artifact.write_bytes(b'{}\n')
        record = {
            "bytes": 3,
            "path": artifact.name,
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        }
        frontier._artifact_bytes(record, self.root, maximum=100)

        forged = copy.deepcopy(record)
        forged["sha256"] = "0" * 64
        with self.assertRaisesRegex(frontier.FrontierError, "SHA-256 mismatch"):
            frontier._artifact_bytes(forged, self.root, maximum=100)

        escaped = copy.deepcopy(record)
        escaped["path"] = "../evidence.json"
        with self.assertRaisesRegex(frontier.FrontierError, "confined"):
            frontier._artifact_bytes(escaped, self.root, maximum=100)

    def test_strict_json_rejects_duplicate_keys(self) -> None:
        with self.assertRaisesRegex(frontier.FrontierError, "duplicate JSON key"):
            frontier._strict_json(b'{"pass":true,"pass":false}', "attack.json")


if __name__ == "__main__":
    unittest.main()
