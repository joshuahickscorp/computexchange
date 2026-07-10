#!/usr/bin/env python3
"""Cheap tests for the CX Cycles fork scaffold."""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import cycles_fork  # noqa: E402


class CyclesForkScaffoldTest(unittest.TestCase):
    def test_manifest_points_at_official_standalone_cycles(self):
        manifest = cycles_fork.scaffold_manifest(root="/tmp/cx-cycles", ref="release/v5.1")
        self.assertEqual(manifest["component"], "cycles")
        self.assertEqual(manifest["remote"], "https://projects.blender.org/blender/cycles.git")
        self.assertEqual(manifest["ref"], "release/v5.1")
        self.assertEqual(manifest["binary"], "/tmp/cx-cycles/install/cycles")

    def test_stage_order_matches_build_contract(self):
        stages = cycles_fork.build_stages(root="/tmp/cx-cycles", ref="main", jobs=8)
        self.assertEqual([s.name for s in stages],
                         ["deps", "clone", "sync_libs", "apply_patches", "build",
                          "binary_smoke", "patch_cli_smoke", "render_smoke"])
        self.assertIn("make update", stages[2].cmd)
        self.assertIn("CX_CYCLES_PATCH_APPLIED", stages[3].cmd)
        self.assertIn("PARALLEL_JOBS=8 make", stages[4].cmd)
        self.assertIn("CX_CYCLES_BINARY_SMOKE_OK=1", stages[5].cmd)
        self.assertIn("--sample-subset-offset", stages[6].cmd)
        self.assertIn("--disable-adaptive-sampling", stages[6].cmd)
        self.assertIn("--cx-batch-manifest", stages[6].cmd)
        self.assertIn("--cx-crop", stages[6].cmd)
        self.assertIn("cx_cycles_batch_manifest.txt", stages[6].cmd)
        self.assertIn("cx_cycles_batch_0.png", stages[6].cmd)
        self.assertIn("cx_cycles_batch_1.png", stages[6].cmd)
        self.assertIn("cx_cycles_batch_crop.png", stages[6].cmd)
        self.assertIn("CX_CYCLES_CROP_SMOKE_OK=1", stages[6].cmd)
        self.assertIn("CX_CYCLES_BATCH_CROP_SMOKE_OK=1", stages[6].cmd)
        self.assertIn("CX_CYCLES_PATCH_CLI_SMOKE_OK=1", stages[6].cmd)
        self.assertIn("examples/scene_monkey.xml", stages[7].cmd)
        self.assertIn("CX_CYCLES_RENDER_SMOKE_OK=1", stages[7].cmd)

    def test_can_disable_patch_stage_for_pristine_upstream_baseline(self):
        stages = cycles_fork.build_stages(
            root="/tmp/cx-cycles",
            ref="main",
            jobs=0,
            apply_patches=False,
        )
        self.assertNotIn("apply_patches", [s.name for s in stages])
        manifest = cycles_fork.scaffold_manifest(
            root="/tmp/cx-cycles",
            ref="main",
            jobs=0,
            apply_patches=False,
        )
        self.assertEqual(manifest["patches"], [])

    def test_commands_do_not_embed_runpod_credentials(self):
        manifest = cycles_fork.scaffold_manifest(root="/tmp/cx-cycles", ref="main", jobs=0)
        blob = json.dumps(manifest)
        self.assertNotIn("RUNPOD_API_KEY", blob)
        self.assertNotIn("rpa_", blob)

    def test_build_stage_accepts_cmake_args(self):
        stages = cycles_fork.build_stages(
            root="/tmp/cx-cycles",
            ref="main",
            jobs=16,
            cmake_args="-DWITH_CYCLES_CUDA_BINARIES=ON -DCYCLES_CUDA_BINARIES_ARCH=sm_89",
        )
        build = next(s for s in stages if s.name == "build")
        self.assertIn("BUILD_CMAKE_ARGS=", build.cmd)
        self.assertIn("WITH_CYCLES_CUDA_BINARIES=ON", build.cmd)
        self.assertIn("PARALLEL_JOBS=16 make", build.cmd)

    def test_runtime_smoke_stages_validate_prebuilt_root(self):
        stages = cycles_fork.runtime_smoke_stages(root="/opt/cx-cycles")
        self.assertEqual([s.name for s in stages],
                         ["binary_smoke", "patch_cli_smoke", "render_smoke"])
        joined = "\n".join(s.cmd for s in stages)
        self.assertIn("/opt/cx-cycles/install/cycles", joined)
        self.assertIn("CX_CYCLES_BINARY_SMOKE_OK=1", joined)
        self.assertIn("--disable-adaptive-sampling", joined)
        self.assertIn("--cx-batch-manifest", joined)
        self.assertIn("--cx-crop", joined)
        self.assertIn("cx_cycles_batch_manifest.txt", joined)
        self.assertIn("CX_CYCLES_CROP_SMOKE_OK=1", joined)
        self.assertIn("CX_CYCLES_BATCH_CROP_SMOKE_OK=1", joined)
        self.assertIn("CX_CYCLES_PATCH_CLI_SMOKE_OK=1", joined)

    def test_shell_quoting_handles_spaces(self):
        stages = cycles_fork.build_stages(
            root="/tmp/cx cycles",
            ref="release/v5.1; echo BAD",
            jobs=0,
        )
        joined = "\n".join(s.cmd for s in stages)
        self.assertIn("'/tmp/cx cycles'", joined)
        self.assertIn("'release/v5.1; echo BAD'", joined)


if __name__ == "__main__":
    unittest.main()
