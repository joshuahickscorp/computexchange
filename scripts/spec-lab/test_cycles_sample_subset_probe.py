#!/usr/bin/env python3
"""Cheap tests for the Cycles sample-subset probe scaffold."""

import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import run_cycles_sample_subset_probe as probe  # noqa: E402


class CyclesSampleSubsetProbeTest(unittest.TestCase):
    def test_probe_command_uses_patched_cli_flags_and_oiiotool(self):
        cmd = probe.subset_probe_cmd("/root/cx-cycles", "scene_monkey.xml", 8, "CUDA")
        self.assertEqual(cmd.count("--device CUDA"), 3)
        self.assertIn("--sample-subset-offset 0 --sample-subset-length 4", cmd)
        self.assertIn("--sample-subset-offset 4 --sample-subset-length 4", cmd)
        self.assertIn("oiiotool", cmd)
        self.assertIn("CX_SUBSET_DIFF_RC", cmd)
        self.assertIn("CX_SUBSET_PROBE_OK", cmd)

    def test_probe_command_can_disable_adaptive_sampling(self):
        cmd = probe.subset_probe_cmd(
            "/root/cx-cycles",
            "scene_monkey.xml",
            64,
            "CUDA",
            disable_adaptive_sampling=True,
        )
        self.assertEqual(cmd.count("--disable-adaptive-sampling"), 3)
        self.assertIn("--device CUDA --disable-adaptive-sampling --samples 64", cmd)

    def test_parse_probe_reads_diff_rc(self):
        rec = probe.parse_probe({
            "ok": True,
            "elapsed_s": 2.0,
            "out_tail": "CX_SUBSET_DIFF_RC=1\nCX_SUBSET_PROBE_OK",
        })
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["diff_rc"], 1)

    def test_skip_build_manifest_uses_prebuilt_root(self):
        out = subprocess.check_output([
            sys.executable,
            os.path.join(HERE, "run_cycles_sample_subset_probe.py"),
            "--dry-run",
            "--skip-build",
            "--remote-root", "/opt/cx-cycles",
            "--device", "CUDA",
            "--samples", "64",
            "--disable-adaptive-sampling",
        ], text=True)
        self.assertIn('"skip_build": true', out)
        self.assertIn('"disable_adaptive_sampling": true', out)
        self.assertIn("/opt/cx-cycles/install/cycles --device CUDA", out)
        self.assertNotIn('"name": "clone"', out)
        self.assertNotIn('"name": "build"', out)


if __name__ == "__main__":
    unittest.main()
