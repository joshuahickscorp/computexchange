#!/usr/bin/env python3
"""Pure tests for the resident raster follow-up report helpers."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import screen_blender_resident_raster_followup as followup  # noqa: E402


class ResidentRasterFollowupTest(unittest.TestCase):
    def test_timing_summary_uses_only_supported_finite_rows(self) -> None:
        rows = [
            {"supported": True, "timings_s": {"total": 0.3}},
            {"supported": False, "timings_s": {"total": 0.001}},
            {"supported": True, "timings_s": {"total": 0.1}},
            {"supported": True, "timings_s": {"total": 0.2}},
        ]
        self.assertEqual(
            followup._summary(rows, "total"),
            {"count": 3, "max_s": 0.3, "median_s": 0.2, "min_s": 0.1},
        )
        self.assertEqual(
            followup._summary(rows, "missing"),
            {"count": 0, "median_s": None, "min_s": None, "max_s": None},
        )

    def test_blender_separator_is_fail_closed(self) -> None:
        self.assertEqual(
            followup._args(
                ["--background", "--", "--child", "--scene", "/tmp/a.blend"]
            ),
            ["--child", "--scene", "/tmp/a.blend"],
        )

    def test_initial_quality_map_is_exact_sha_keyed(self) -> None:
        quality = {"summary": {"pass": False}}
        initial = {
            "arms": [
                {
                    "artifact": {"sha256": "a" * 64},
                    "quality_v3": quality,
                },
                {"artifact": {"sha256": "b" * 64}},
            ]
        }
        self.assertEqual(
            followup._initial_quality_map(initial), {"a" * 64: quality}
        )


if __name__ == "__main__":
    unittest.main()
