#!/usr/bin/env python3
"""Cheap tests for the Cycles CUDA/OptiX diagnostic driver."""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import run_cycles_device_diag as diag  # noqa: E402


class CyclesDeviceDiagTest(unittest.TestCase):
    def test_diagnostic_stage_captures_gpu_crash_context(self):
        stage = diag.diagnostic_stage("/root/cx-cycles")
        self.assertEqual(stage.name, "device_diag")
        self.assertIn("nvidia-smi", stage.cmd)
        self.assertIn("--list-devices", stage.cmd)
        self.assertIn("--device CUDA", stage.cmd)
        self.assertIn("thread apply all bt", stage.cmd)
        self.assertIn("strace", stage.cmd)
        self.assertIn("CX_CYCLES_DEVICE_DIAG_OK=1", stage.cmd)

    def test_diagnostic_stage_does_not_embed_runpod_credentials(self):
        stage = diag.diagnostic_stage("/root/cx-cycles")
        self.assertNotIn("RUNPOD_API_KEY", stage.cmd)
        self.assertNotIn("rpa_", json.dumps(stage.to_json()))


if __name__ == "__main__":
    unittest.main()
