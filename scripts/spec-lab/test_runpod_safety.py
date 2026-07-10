#!/usr/bin/env python3
"""Cheap safety tests for the RunPod lifecycle helper."""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import runpod  # noqa: E402


class RunpodSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_state_file = runpod.STATE_FILE
        self.old_gql = runpod.gql
        self.old_env_file = runpod.DEFAULT_RUNPOD_ENV_FILE
        self.old_ssh = runpod.ssh
        self.old_scp_to = runpod.scp_to
        self.old_deploy = runpod._deploy
        self.old_ssh_endpoint = runpod._ssh_endpoint
        self.old_ssh_ok = runpod._ssh_ok
        self.old_cuda_ok = runpod._cuda_ok
        self.old_cuda_driver_version = runpod._cuda_driver_version
        runpod.STATE_FILE = os.path.join(self.tmp.name, "tracked.json")
        runpod.DEFAULT_RUNPOD_ENV_FILE = os.path.join(self.tmp.name, "runpod.env")

    def tearDown(self):
        runpod.gql = self.old_gql
        runpod.STATE_FILE = self.old_state_file
        runpod.DEFAULT_RUNPOD_ENV_FILE = self.old_env_file
        runpod.ssh = self.old_ssh
        runpod.scp_to = self.old_scp_to
        runpod._deploy = self.old_deploy
        runpod._ssh_endpoint = self.old_ssh_endpoint
        runpod._ssh_ok = self.old_ssh_ok
        runpod._cuda_ok = self.old_cuda_ok
        runpod._cuda_driver_version = self.old_cuda_driver_version
        self.tmp.cleanup()

    def test_failed_terminate_keeps_pod_tracked_for_retry(self):
        runpod._save_tracked(["pod-a"])

        def fail_gql(*_args, **_kwargs):
            raise RuntimeError("connection reset")

        runpod.gql = fail_gql
        self.assertFalse(runpod.terminate("pod-a"))
        self.assertEqual(runpod._load_tracked(), ["pod-a"])

    def test_successful_terminate_untracks_pod(self):
        runpod._save_tracked(["pod-a"])

        def ok_gql(*_args, **_kwargs):
            return {"podTerminate": True}

        runpod.gql = ok_gql
        self.assertTrue(runpod.terminate("pod-a"))
        self.assertEqual(runpod._load_tracked(), [])

    def test_api_key_loads_from_ignored_env_file(self):
        with open(runpod.DEFAULT_RUNPOD_ENV_FILE, "w") as f:
            f.write("RUNPOD_API_KEY=rpa_test_secret\n")
        old = os.environ.pop("RUNPOD_API_KEY", None)
        try:
            self.assertEqual(runpod._api_key(), "rpa_test_secret")
        finally:
            if old is not None:
                os.environ["RUNPOD_API_KEY"] = old

    def test_redact_secret_scrubs_key_shapes(self):
        self.assertNotIn(
            "rpa_test_secret",
            runpod.redact_secret("Authorization: Bearer rpa_test_secret api_key=rpa_test_secret"),
        )

    def test_remote_watchdog_still_arms(self):
        calls = []
        old = os.environ.get("RUNPOD_API_KEY")
        os.environ["RUNPOD_API_KEY"] = "rpa_test_secret"

        def fake_ssh(pod, cmd, timeout=1200):
            calls.append((pod, cmd, timeout))
            return 0, "", ""

        def fake_scp_to(pod, local, remote, timeout=300):
            calls.append((pod, f"scp {local} {remote}", timeout))
            with open(local) as f:
                self.assertIn("Authorization: Bearer rpa_test_secret", f.read())
            return True, ""

        runpod.ssh = fake_ssh
        runpod.scp_to = fake_scp_to
        try:
            runpod.arm_remote_watchdog({"id": "pod-a", "ip": "127.0.0.1", "port": 22}, 60)
        finally:
            if old is None:
                os.environ.pop("RUNPOD_API_KEY", None)
            else:
                os.environ["RUNPOD_API_KEY"] = old
        self.assertEqual(len(calls), 4)
        self.assertIn("podTerminate", calls[-1][1])
        self.assertIn("sleep 60", calls[-1][1])
        self.assertNotIn("rpa_test_secret", calls[-1][1])
        self.assertIn("@/root/.cx_runpod_watchdog_auth", calls[-1][1])

    def test_live_pods_query_returns_my_pods(self):
        def fake_gql(query, *_args, **_kwargs):
            self.assertIn("pods", query)
            return {"myself": {"pods": [{"id": "pod-a"}]}}

        runpod.gql = fake_gql
        self.assertEqual(runpod.live_pods(), [{"id": "pod-a"}])

    def test_provision_does_not_cycle_gpu_plan_downward(self):
        attempted = []

        def fake_deploy(gpu_type, cloud, image, disk_gb, name):
            attempted.append((gpu_type, cloud))
            return None

        runpod._deploy = fake_deploy
        try:
            with self.assertRaisesRegex(RuntimeError, "after 2 deploys"):
                runpod.provision_reachable(
                    [("cheap", "COMMUNITY"), ("better", "SECURE")],
                    image="img",
                    max_deploys=5,
                )
        finally:
            runpod._deploy = self.old_deploy
        self.assertEqual(attempted, [("cheap", "COMMUNITY"), ("better", "SECURE")])

    def test_provision_uses_full_plan_by_default(self):
        attempted = []

        def fake_deploy(gpu_type, cloud, image, disk_gb, name):
            attempted.append((gpu_type, cloud))
            return None

        runpod._deploy = fake_deploy
        try:
            with self.assertRaisesRegex(RuntimeError, "after 3 deploys"):
                runpod.provision_reachable(
                    [("cheap", "COMMUNITY"), ("better", "SECURE"), ("best", "SECURE")],
                    image="img",
                )
        finally:
            runpod._deploy = self.old_deploy
        self.assertEqual(
            attempted,
            [("cheap", "COMMUNITY"), ("better", "SECURE"), ("best", "SECURE")],
        )

    def test_provision_rejects_pods_below_min_driver_and_upgrades(self):
        deployed = []
        terminated = []

        def fake_deploy(gpu_type, cloud, image, disk_gb, name):
            deployed.append((gpu_type, cloud))
            return f"pod-{len(deployed)}"

        def fake_endpoint(pod_id):
            return ("127.0.0.1", 2200 + int(pod_id.split("-")[-1]))

        def fake_driver(ip, port):
            return 12080 if port == 2201 else 13000

        def fake_terminate(pod_id):
            terminated.append(pod_id)
            runpod._untrack(pod_id)
            return True

        runpod._deploy = fake_deploy
        runpod._ssh_endpoint = fake_endpoint
        runpod._ssh_ok = lambda *_args: True
        runpod._cuda_ok = lambda *_args: True
        runpod._cuda_driver_version = fake_driver
        old_terminate = runpod.terminate
        runpod.terminate = fake_terminate
        try:
            pod = runpod.provision_reachable(
                [("l40s", "SECURE"), ("h100", "SECURE")],
                image="img",
                min_cuda_driver_version=13000,
            )
        finally:
            runpod.terminate = old_terminate
        self.assertEqual(pod["gpu"], "h100")
        self.assertEqual(pod["cuda_driver_version"], 13000)
        self.assertEqual(terminated, ["pod-1"])


if __name__ == "__main__":
    unittest.main()
