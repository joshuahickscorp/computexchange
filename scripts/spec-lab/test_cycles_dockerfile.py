#!/usr/bin/env python3
"""Cheap checks for the prebuilt Cycles image scaffold."""

import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DOCKERFILE = os.path.join(REPO, "docker", "cycles", "Dockerfile")


class CyclesDockerfileTest(unittest.TestCase):
    def test_dockerfile_builds_patched_cycles(self):
        with open(DOCKERFILE) as f:
            text = f.read()
        self.assertIn("projects.blender.org/blender/cycles.git", text)
        self.assertIn("patches/cycles/*.patch", text)
        self.assertIn("--sample-subset-offset", text)
        self.assertIn("WITH_CYCLES_CUDA_BINARIES=ON", text)
        self.assertIn("WITH_CYCLES_DEVICE_OPTIX=OFF", text)
        self.assertIn("CYCLES_CUDA_BINARIES_ARCH", text)
        self.assertIn("PARALLEL_JOBS", text)
        self.assertIn("openimageio-tools", text)


if __name__ == "__main__":
    unittest.main()
