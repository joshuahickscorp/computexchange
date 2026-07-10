#!/usr/bin/env python3
"""Cheap tests for the standalone Cycles quality-ladder driver."""

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import run_cycles_quality_ladder as ladder  # noqa: E402


class CyclesQualityLadderTest(unittest.TestCase):
    def test_quality_ladder_command_renders_reference_and_drafts(self):
        cmd = ladder.quality_ladder_cmd(
            "/root/cx-cycles",
            "scene_world_volume.xml",
            4096,
            [64, 128],
            "CUDA",
            True,
        )
        self.assertIn("CX_QUALITY_SCENE=scene_world_volume.xml", cmd)
        self.assertIn("--device CUDA --disable-adaptive-sampling --samples 4096", cmd)
        self.assertIn("CX_DRAFT_TIME_S_64", cmd)
        self.assertIn("CX_DRAFT_TIME_S_128", cmd)
        self.assertIn("CX_QUALITY_ROW=", cmd)
        self.assertIn("worst_tile_ssim", cmd)
        self.assertIn("CX_QUALITY_LADDER_OK", cmd)

    def test_quality_ladder_command_can_run_oidn_variant(self):
        cmd = ladder.quality_ladder_cmd(
            "/root/cx-cycles",
            "scene_monkey.xml",
            4096,
            [8],
            "CUDA",
            True,
            with_oidn=True,
            oidn_device="cpu",
        )
        self.assertIn("libOpenImageDenoise.so.2", cmd)
        self.assertIn("OIDN_DEVICE_TYPE_CPU", cmd)
        self.assertIn("/tmp/cx_quality_time_oidn_8.txt", cmd)
        self.assertIn("variant", cmd)
        self.assertIn('"oidn"', cmd)

    def test_parse_quality_rows(self):
        row = {
            "scene": "scene_cube_volume.xml",
            "samples": 256,
            "quality": 0.95,
            "worst_tile_ssim": 0.9,
            "tier": "preview",
        }
        stage = {
            "out_tail": "\n".join([
                "noise",
                "CX_QUALITY_ROW=" + json.dumps(row, sort_keys=True),
                "CX_QUALITY_LADDER_OK",
            ])
        }
        self.assertEqual(ladder.parse_quality_rows(stage), [row])

    def test_summarize_prefers_fastest_passing_tier(self):
        rows = [
            {
                "scene": "a.xml",
                "samples": 64,
                "quality": 0.91,
                "worst_tile_ssim": 0.86,
                "speedup_vs_ref": 10.0,
                "tier": "preview",
            },
            {
                "scene": "a.xml",
                "samples": 256,
                "quality": 0.985,
                "worst_tile_ssim": 0.96,
                "speedup_vs_ref": 4.0,
                "tier": "delivery",
            },
        ]
        summary = ladder.summarize(rows)
        self.assertEqual(summary["best_preview"]["samples"], 64)
        self.assertEqual(summary["best_delivery"]["samples"], 256)

    def test_transfer_preflight_cuts_slow_upload(self):
        old_scp = ladder.runpod.scp_to
        old_ssh = ladder.runpod.ssh
        old_ledger = ladder.LEDGER

        def fake_scp(_pod, _local, _remote, timeout=300):
            return True, ""

        def fake_ssh(_pod, _cmd, timeout=1200):
            return 0, "", ""

        ladder.runpod.scp_to = fake_scp
        ladder.runpod.ssh = fake_ssh
        with tempfile.TemporaryDirectory() as td:
            ladder.LEDGER = os.path.join(td, "cycles_quality_ladder_ledger.jsonl")
            try:
                rec = ladder.transfer_preflight(
                    {"id": "pod-a", "ip": "127.0.0.1", "port": 22},
                    size_mb=1,
                    min_mbps=1000000.0,
                    timeout_s=5,
                )
            finally:
                ladder.runpod.scp_to = old_scp
                ladder.runpod.ssh = old_ssh
                ladder.LEDGER = old_ledger
        self.assertEqual(rec["stage"], "transfer_preflight")
        self.assertFalse(rec["ok"])
        self.assertIn("mbps", rec)

    def test_ada_plan_includes_reachable_alternatives(self):
        plan = ladder.gpu_plan_for_tier("ada")
        self.assertIn(("NVIDIA GeForce RTX 4090", "COMMUNITY"), plan)
        self.assertIn(("NVIDIA L40S", "SECURE"), plan)
        self.assertIn(("NVIDIA L4", "SECURE"), plan)


if __name__ == "__main__":
    unittest.main()
