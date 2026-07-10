#!/usr/bin/env python3
"""Cheap tests for carried CX Cycles patch files."""

import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PATCH = os.path.join(
    REPO,
    "patches",
    "cycles",
    "0001-standalone-sample-subset-cli.patch",
)
OIDN_PATCH = os.path.join(
    REPO,
    "patches",
    "cycles",
    "0002-device-skip-oidn-cuda-probe-by-default.patch",
)
ADAPTIVE_PATCH = os.path.join(
    REPO,
    "patches",
    "cycles",
    "0003-standalone-disable-adaptive-sampling-cli.patch",
)
BATCH_PATCH = os.path.join(
    REPO,
    "patches",
    "cycles",
    "0004-standalone-cx-batch-manifest.patch",
)
CROP_PATCH = os.path.join(
    REPO,
    "patches",
    "cycles",
    "0005-standalone-cx-crop-cli.patch",
)
BATCH_CROP_PATCH = os.path.join(
    REPO,
    "patches",
    "cycles",
    "0006-standalone-cx-batch-crop-manifest.patch",
)


class CyclesPatchTest(unittest.TestCase):
    def test_sample_subset_patch_exposes_cli_flags(self):
        with open(PATCH) as f:
            text = f.read()
        self.assertIn("--sample-subset-offset", text)
        self.assertIn("--sample-subset-length", text)
        self.assertIn("use_sample_subset = true", text)
        self.assertIn("src/app/cycles_standalone.cpp", text)

    def test_oidn_cuda_probe_patch_is_opt_in(self):
        with open(OIDN_PATCH) as f:
            text = f.read()
        self.assertIn("CX_CYCLES_PROBE_OIDN_CUDA", text)
        self.assertIn("oidnIsCUDADeviceSupported", text)
        self.assertIn("src/device/cuda/device.cpp", text)

    def test_adaptive_sampling_patch_exposes_fixed_sample_flag(self):
        with open(ADAPTIVE_PATCH) as f:
            text = f.read()
        self.assertIn("--disable-adaptive-sampling", text)
        self.assertIn("set_use_adaptive_sampling(false)", text)
        self.assertIn("src/app/cycles_standalone.cpp", text)

    def test_batch_manifest_patch_scaffolds_resident_jobs(self):
        with open(BATCH_PATCH) as f:
            text = f.read()
        self.assertIn("--cx-batch-manifest", text)
        self.assertIn("CX_CYCLES_BATCH_JOB_OK", text)
        self.assertIn("CX_CYCLES_BATCH_OK", text)
        self.assertIn("set_output_driver", text)
        self.assertIn("options.session->start()", text)
        self.assertIn("options.session->wait()", text)

    def test_crop_patch_exposes_buffer_coordinate_cli(self):
        with open(CROP_PATCH) as f:
            text = f.read()
        self.assertIn("--cx-crop", text)
        self.assertIn("cx_crop_full_width", text)
        self.assertIn("buffer_params.full_x", text)
        self.assertIn("set_full_width(camera_width)", text)
        self.assertIn("update_offset_stride", text)

    def test_batch_crop_patch_keeps_resident_crop_jobs(self):
        with open(BATCH_CROP_PATCH) as f:
            text = f.read()
        self.assertIn("[CROP_X CROP_Y CROP_W CROP_H FULL_W FULL_H]", text)
        self.assertIn("job.cx_crop_enabled = false", text)
        self.assertIn("options.cx_crop_enabled = job.cx_crop_enabled", text)
        self.assertIn("crop=%d", text)


if __name__ == "__main__":
    unittest.main()
