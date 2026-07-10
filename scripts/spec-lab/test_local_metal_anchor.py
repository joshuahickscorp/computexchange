#!/usr/bin/env python3
"""Unit tests for the LOCAL Metal render lane driver plumbing.

Everything here runs WITHOUT Blender and WITHOUT network: blender discovery is
tested against temp files, config building against the documented defaults,
ledger append against a temp dir, and the shim against compile() + content
checks. The one subprocess-shaped seam (deps_missing) is tested with an
injected fake runner.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import run_local_metal_anchor as lane  # noqa: E402


class DiscoverBlenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cx-lane-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_bin(self, name):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(path, 0o755)
        return path

    def test_explicit_path_wins(self):
        b = self._fake_bin("blender-explicit")
        got = lane.discover_blender(explicit=b, environ={}, candidates=(),
                                    which=lambda _n: None)
        self.assertEqual(got, b)

    def test_env_var_beats_candidates(self):
        env_b = self._fake_bin("blender-env")
        cand_b = self._fake_bin("blender-cand")
        got = lane.discover_blender(environ={lane.BLENDER_ENV_VAR: env_b},
                                    candidates=(cand_b,), which=lambda _n: None)
        self.assertEqual(got, env_b)

    def test_candidate_then_path_fallback(self):
        cand_b = self._fake_bin("blender-cand")
        got = lane.discover_blender(environ={}, candidates=(cand_b,),
                                    which=lambda _n: None)
        self.assertEqual(got, cand_b)
        path_b = self._fake_bin("blender-path")
        got = lane.discover_blender(environ={}, candidates=(),
                                    which=lambda _n: path_b)
        self.assertEqual(got, path_b)

    def test_non_executable_and_missing_are_rejected(self):
        dead = os.path.join(self.tmp, "not-exec")
        with open(dead, "w") as f:
            f.write("x")
        os.chmod(dead, 0o644)
        got = lane.discover_blender(explicit=dead, environ={}, candidates=(),
                                    which=lambda _n: None)
        self.assertIsNone(got)
        got = lane.discover_blender(explicit=os.path.join(self.tmp, "ghost"),
                                    environ={}, candidates=(),
                                    which=lambda _n: None)
        self.assertIsNone(got)


class BuildConfigTest(unittest.TestCase):
    def test_tiny_local_defaults(self):
        cfg = lane.build_config()
        self.assertEqual(cfg["scene"], "classroom")
        self.assertEqual(cfg["resolution"], "960x540")
        self.assertEqual(cfg["frames"], 2)          # runner minimum, documented
        self.assertEqual(cfg["keyframe_every"], 1)  # all-anchor, no reprojection
        self.assertEqual(cfg["ref_spp"], 512)
        self.assertEqual(cfg["draft_spp"], 64)
        self.assertFalse(cfg["repair_enabled"])
        self.assertEqual(cfg["hole_fill"], "inpaint")  # kf=1: fully measured
        self.assertTrue(cfg["require_gpu"])            # fail-loud, never silent CPU
        self.assertEqual(cfg["device"], "GPU")

    def test_overrides_merge_and_none_is_ignored(self):
        cfg = lane.build_config({"ref_spp": 128, "scene": None})
        self.assertEqual(cfg["ref_spp"], 128)
        self.assertEqual(cfg["scene"], "classroom")

    def test_frames_clamped_to_runner_minimum(self):
        cfg = lane.build_config({"frames": 1})
        self.assertEqual(cfg["frames"], 2)

    def test_config_is_json_serializable(self):
        json.dumps(lane.build_config())


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cx-lane-ledger-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_row_carries_evidence_label_and_payloads(self):
        metrics = {"net_speedup": 3.2, "device": "GPU/METAL"}
        cfg = lane.build_config()
        row = lane.ledger_row(metrics, cfg, {"machine": "arm64"})
        self.assertEqual(row["event"], "local_metal_anchor_receipt")
        self.assertEqual(row["evidence"], "MEASURED/local-metal")
        self.assertEqual(row["row"], metrics)
        self.assertEqual(row["config"], cfg)
        self.assertIn("T", row["ts"])  # ISO timestamp

    def test_append_creates_parents_and_appends_jsonl(self):
        path = os.path.join(self.tmp, "deep", "nested", "ledger.jsonl")
        r1 = lane.ledger_row({"a": 1}, {}, {})
        r2 = lane.ledger_row({"a": 2}, {}, {})
        lane.append_ledger(path, r1)
        lane.append_ledger(path, r2)
        with open(path) as f:
            lines = [json.loads(ln) for ln in f.read().strip().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["row"], {"a": 1})
        self.assertEqual(lines[1]["row"], {"a": 2})


class ShimTest(unittest.TestCase):
    def test_shim_source_compiles(self):
        compile(lane.SHIM_SOURCE, "cx_local_shim.py", "exec")

    def test_shim_patches_every_pod_rooted_constant(self):
        for const in ("BLENDER_DIR", "BLENDER_BIN", "WORK_DIR",
                      "_CACHE_ROOT", "SCENES_DIR"):
            self.assertIn(f"ers.{const}", lane.SHIM_SOURCE,
                          f"shim must repoint ers.{const}")

    def test_shim_mirrors_runner_error_contract(self):
        # failures must emit {"error":...} and exit 0 — never a fabricated number.
        self.assertIn('ers.emit({"error"', lane.SHIM_SOURCE)
        self.assertIn("sys.exit(0)", lane.SHIM_SOURCE)


class ParseLastJsonLineTest(unittest.TestCase):
    def test_picks_last_json_object_among_noise(self):
        text = ("human log line\n"
                '{"first": 1}\n'
                "more logs {not json}\n"
                '{"net_speedup": 4.2, "device": "GPU/METAL"}\n')
        got = lane.parse_last_json_line(text)
        self.assertEqual(got["net_speedup"], 4.2)

    def test_none_when_no_json(self):
        self.assertIsNone(lane.parse_last_json_line("no json here\n"))
        self.assertIsNone(lane.parse_last_json_line(""))


class DepsMissingTest(unittest.TestCase):
    def test_uses_injected_runner_and_parses_stdout(self):
        class FakeProc:
            stdout = '["OpenEXR"]\n'
            returncode = 0

        calls = {}

        def fake_runner(cmd, **kw):
            calls["cmd"] = cmd
            return FakeProc()

        got = lane.deps_missing("/fake/python", runner=fake_runner)
        self.assertEqual(got, ["OpenEXR"])
        self.assertEqual(calls["cmd"][0], "/fake/python")

    def test_unprobeable_python_reports_everything_missing(self):
        def broken_runner(_cmd, **_kw):
            raise OSError("no such interpreter")

        got = lane.deps_missing("/fake/python", runner=broken_runner)
        self.assertEqual(got, list(lane.REQUIRED_IMPORTS))


class LocalOnlySafetyTest(unittest.TestCase):
    """HARD RULE: this lane is LOCAL ONLY — it must never touch RunPod."""

    def test_driver_source_never_uses_runpod(self):
        # the docstring may STATE "no RunPod API is ever called"; what must never
        # appear is an actual usage: the client import, the API host, or the key.
        with open(os.path.join(HERE, "run_local_metal_anchor.py")) as f:
            src = f.read()
        self.assertNotIn("import runpod", src)
        self.assertNotIn("api.runpod.io", src)
        self.assertNotIn("RUNPOD_API_KEY", src)
        self.assertNotIn("rpa_", src)

    def test_default_ledger_lives_in_the_assigned_report_dir(self):
        self.assertTrue(lane.DEFAULT_LEDGER.endswith(
            os.path.join("docs", "speed-lane-reports", "spec-lab",
                         "local_metal_ledger.jsonl")))


class MetalLadderGuardTest(unittest.TestCase):
    """The ONLY allowed edit to pod/exp_render_stack.py: the guarded METAL rung."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(HERE, "pod", "exp_render_stack.py")) as f:
            cls.src = f.read()

    def test_metal_is_the_last_rung_in_both_ladders(self):
        self.assertEqual(
            self.src.count("('OPTIX', 'CUDA', 'HIP', 'ONEAPI', 'METAL')"), 2,
            "both the scene-script ladder and the GPU-probe ladder need METAL")
        # no ladder without METAL remains
        self.assertNotIn("('OPTIX', 'CUDA', 'HIP', 'ONEAPI')", self.src)

    def test_metal_rung_is_guarded_with_hasattr(self):
        # the macOS headless gotcha fix must be attribute-guarded so non-Metal
        # builds are a strict no-op.
        self.assertEqual(
            self.src.count("hasattr(prefs, 'get_devices_for_type')"), 2)
        self.assertEqual(
            self.src.count("prefs.get_devices_for_type('METAL')"), 2)

    def test_fail_loud_paths_survive(self):
        # require_gpu must still refuse silent CPU: both sentinels intact.
        self.assertIn("CX_DEVICE_ERROR=", self.src)
        self.assertIn("CX_GPU_PROBE_ERROR=", self.src)


if __name__ == "__main__":
    unittest.main()
