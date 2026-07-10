#!/usr/bin/env python3
"""Cheap tests for the standalone Cycles benchmark matrix scaffold."""

import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import run_cycles_baseline_matrix as matrix  # noqa: E402


class CyclesBaselineMatrixTest(unittest.TestCase):
    def test_benchmark_command_renders_official_example(self):
        cmd = matrix.benchmark_cmd("/root/cx-cycles", "scene_monkey.xml", 32, "CUDA")
        self.assertIn("/root/cx-cycles/install/cycles", cmd)
        self.assertIn("--device CUDA", cmd)
        self.assertIn("--samples 32", cmd)
        self.assertIn("examples/scene_monkey.xml", cmd)
        self.assertIn("CX_TIME_S=", cmd)
        self.assertIn("CX_BENCH_OK", cmd)

    def test_device_inventory_command_is_nonfatal(self):
        cmd = matrix.device_inventory_cmd("/root/cx-cycles")
        self.assertIn("--list-devices", cmd)
        self.assertIn("|| true", cmd)
        self.assertIn("CX_DEVICES_OK=1", cmd)

    def test_parse_bench_extracts_time(self):
        stage = {
            "ok": True,
            "elapsed_s": 1.2,
            "out_tail": "CX_TIME_S=0.91\n-rw-r--r-- 1 root root 314K Jul 8 00:00 /tmp/x.png\nCX_BENCH_OK",
        }
        rec = matrix.parse_bench(stage, "scene_monkey.xml", 8, "OPTIX")
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["cycles_time_s"], 0.91)
        self.assertEqual(rec["samples"], 8)
        self.assertEqual(rec["device"], "OPTIX")

    def test_skip_build_manifest_uses_runtime_smokes_only(self):
        out = subprocess.check_output([
            sys.executable,
            os.path.join(HERE, "run_cycles_baseline_matrix.py"),
            "--dry-run",
            "--skip-build",
            "--remote-root", "/opt/cx-cycles",
            "--scenes", "scene_monkey.xml",
            "--samples", "8",
            "--devices", "CUDA",
        ], text=True)
        self.assertIn('"skip_build": true', out)
        self.assertIn('"name": "binary_smoke"', out)
        self.assertIn('"name": "patch_cli_smoke"', out)
        self.assertNotIn('"name": "clone"', out)
        self.assertNotIn('"name": "build"', out)


if __name__ == "__main__":
    unittest.main()
