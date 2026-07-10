#!/usr/bin/env python3
"""Cheap tests for the CX render autoprobe scaffold."""

import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cx_render_autoprobe as autoprobe  # noqa: E402


class CxRenderAutoprobeTest(unittest.TestCase):
    def test_runtime_role_classification(self):
        self.assertEqual(
            autoprobe.runtime_role("cx-cycles-hopper-sm90-batch-runtime-20260708.tar.gz"),
            "hopper_sm90_batch",
        )
        self.assertEqual(
            autoprobe.runtime_role("cx-cycles-ada-sm89-batch-runtime-20260708.tar.gz"),
            "ada_sm89_batch",
        )

    def test_choose_route_prefers_local_cuda(self):
        route = autoprobe.choose_route(
            {"is_apple_silicon": True},
            {"gpus": [{"name": "NVIDIA L40S", "compute_capability": "8.9"}]},
            {"runpod_api_key_present": False, "ssh_pubkey_present": False},
        )
        self.assertEqual(route["lane"], "local_cuda")
        self.assertEqual(route["runtime_role"], "ada_sm89_batch")

    def test_cloud_state_does_not_expose_secret(self):
        old = os.environ.get("RUNPOD_API_KEY")
        os.environ["RUNPOD_API_KEY"] = "rpa_secret_should_not_escape"
        try:
            state = autoprobe.cloud_state(check_api=False)
        finally:
            if old is None:
                os.environ.pop("RUNPOD_API_KEY", None)
            else:
                os.environ["RUNPOD_API_KEY"] = old
        self.assertTrue(state["runpod_api_key_present"])
        self.assertNotIn("rpa_secret", repr(state))


if __name__ == "__main__":
    unittest.main()
