#!/usr/bin/env python3
"""Cheap tests for the Cycles N-way sample fan-out harness."""

import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import run_cycles_sample_fanout_matrix as fanout  # noqa: E402


class CyclesSampleFanoutMatrixTest(unittest.TestCase):
    def test_ada_plan_includes_reachable_alternatives(self):
        self.assertIn(("NVIDIA L40", "COMMUNITY"), fanout.ADA_GPU_PLAN)
        self.assertIn(("NVIDIA RTX 6000 Ada Generation", "SECURE"), fanout.ADA_GPU_PLAN)
        self.assertIn(("NVIDIA L4", "SECURE"), fanout.ADA_GPU_PLAN)

    def test_fanout_command_renders_full_and_all_subsets(self):
        cmd = fanout.fanout_probe_cmd(
            "/root/cx-cycles",
            "scene_monkey.xml",
            64,
            4,
            "CUDA",
        )
        self.assertIn("--device CUDA --samples 64 --output", cmd)
        self.assertIn("--sample-subset-offset 0 --sample-subset-length 16", cmd)
        self.assertIn("--sample-subset-offset 16 --sample-subset-length 16", cmd)
        self.assertIn("--sample-subset-offset 32 --sample-subset-length 16", cmd)
        self.assertIn("--sample-subset-offset 48 --sample-subset-length 16", cmd)
        self.assertIn("CX_SUBSET_TIME_S_3", cmd)
        self.assertIn("CX_MERGE_TIME_S", cmd)
        self.assertIn("CX_FANOUT_DIFF_RC", cmd)

    def test_fanout_command_can_disable_adaptive_sampling(self):
        cmd = fanout.fanout_probe_cmd(
            "/root/cx-cycles",
            "scene_monkey.xml",
            128,
            2,
            "CUDA",
            disable_adaptive_sampling=True,
        )
        self.assertEqual(cmd.count("--disable-adaptive-sampling"), 3)
        self.assertIn("--device CUDA --disable-adaptive-sampling --samples 128", cmd)

    def test_fanout_command_can_render_more_chunks_than_workers(self):
        cmd = fanout.fanout_probe_cmd(
            "/root/cx-cycles",
            "scene_monkey.xml",
            252,
            4,
            "CUDA",
            chunks_per_worker=3,
            merge_mode="auto",
        )
        self.assertIn("CX_WORKER_COUNT=4", cmd)
        self.assertIn("CX_CHUNK_COUNT=12", cmd)
        self.assertIn("CX_CHUNK_SAMPLE_LENGTH=21", cmd)
        self.assertIn("--sample-subset-offset 231 --sample-subset-length 21", cmd)
        self.assertIn("CX_MERGE_LINEAR_TIME_S", cmd)
        self.assertIn("CX_MERGE_TREE_TIME_S", cmd)
        self.assertIn("CX_MERGE_PYTHON_TIME_S", cmd)
        self.assertIn("CX_MERGE_SELECTED", cmd)
        self.assertIn("OpenImageIO as oiio", cmd)
        self.assertIn("CX_QUALITY_WORST_TILE_SSIM", cmd)

    def test_fanout_command_can_render_chunks_in_parallel(self):
        cmd = fanout.fanout_probe_cmd(
            "/root/cx-cycles",
            "scene_monkey.xml",
            256,
            4,
            "CUDA",
            chunks_per_worker=2,
            execution_mode="parallel",
            parallel_slots=3,
            subset_retries=2,
        )
        self.assertIn("CX_SUBSET_EXECUTION_MODE=parallel", cmd)
        self.assertIn("CX_PARALLEL_SLOTS=3", cmd)
        self.assertIn("CX_SUBSET_RETRIES=2", cmd)
        self.assertIn("subprocess.Popen", cmd)
        self.assertIn("CX_SUBSET_PHASE_WALL_S", cmd)
        self.assertIn("len(running) < parallel_slots", cmd)
        self.assertIn("CX_SUBSET_RETRY", cmd)
        self.assertIn("CX_SUBSET_ATTEMPT_TIME_S", cmd)
        self.assertIn("missing-output", cmd)
        self.assertIn("os.path.getsize(outputs[index])", cmd)

    def test_fanout_command_can_render_chunks_as_batch_manifests(self):
        cmd = fanout.fanout_probe_cmd(
            "/root/cx-cycles",
            "scene_world_volume.xml",
            1536,
            4,
            "CUDA",
            disable_adaptive_sampling=True,
            chunks_per_worker=3,
            execution_mode="batch",
            parallel_slots=2,
            subset_retries=1,
            merge_mode="python",
        )
        self.assertIn("CX_SUBSET_EXECUTION_MODE=batch", cmd)
        self.assertIn("CX_CHUNK_COUNT=12", cmd)
        self.assertIn("CX_PARALLEL_SLOTS=2", cmd)
        self.assertIn("assignments[index % fanout].append(index)", cmd)
        self.assertIn("--cx-batch-manifest", cmd)
        self.assertIn("CX_BATCH_WORKER_TIME_S_", cmd)
        self.assertIn("CX_BATCH_MANIFEST_OK", cmd)
        self.assertIn("CX_MERGE_PYTHON_TIME_S", cmd)

    def test_fanout_command_rejects_non_divisible_samples(self):
        with self.assertRaises(ValueError):
            fanout.fanout_probe_cmd("/root/cx-cycles", "scene_monkey.xml", 65, 8, "CUDA")

    def test_fanout_command_rejects_unknown_execution_mode(self):
        with self.assertRaises(ValueError):
            fanout.fanout_probe_cmd(
                "/root/cx-cycles",
                "scene_monkey.xml",
                256,
                4,
                "CUDA",
                execution_mode="telepathy",
            )

    def test_fanout_command_rejects_non_divisible_chunk_count(self):
        with self.assertRaises(ValueError):
            fanout.fanout_probe_cmd(
                "/root/cx-cycles",
                "scene_monkey.xml",
                256,
                4,
                "CUDA",
                chunks_per_worker=3,
            )

    def test_parse_probe_computes_ideal_parallel_wall(self):
        stage = {
            "ok": True,
            "elapsed_s": 10.0,
            "out_tail": "\n".join([
                "CX_FULL_TIME_S=8.0",
                "CX_SUBSET_TIME_S_0=2.0",
                "CX_SUBSET_TIME_S_1=2.5",
                "CX_SUBSET_TIME_S_2=2.1",
                "CX_SUBSET_TIME_S_3=2.2",
                "CX_MERGE_TIME_S=0.3",
                "CX_FANOUT_DIFF_RC=0",
                "CX_FANOUT_PROBE_OK",
            ]),
        }
        rec = fanout.parse_probe(stage, "scene_monkey.xml", 64, 4, "CUDA")
        self.assertTrue(rec["ok"])
        self.assertTrue(rec["exact"])
        self.assertTrue(rec["numeric_equivalent"])
        self.assertEqual(rec["diff_class"], "exact")
        self.assertEqual(rec["subset_times_s"], [2.0, 2.5, 2.1, 2.2])
        self.assertEqual(rec["ideal_parallel_wall_s"], 2.8)
        self.assertEqual(rec["ideal_speedup_vs_full"], 2.8571)
        self.assertEqual(rec["static_chunk_wall_s"], 2.5)
        self.assertEqual(rec["dynamic_chunk_wall_s"], 2.5)
        self.assertEqual(rec["chunk_scheduler_gain_vs_static"], 1.0)

    def test_parse_probe_models_dynamic_chunk_scheduler(self):
        stage = {
            "ok": True,
            "elapsed_s": 10.0,
            "out_tail": "\n".join([
                "CX_FULL_TIME_S=20.0",
                "CX_CHUNK_COUNT=8",
                "CX_CHUNK_SAMPLE_LENGTH=512",
                "CX_SUBSET_TIME_S_0=5.0",
                "CX_SUBSET_TIME_S_1=1.0",
                "CX_SUBSET_TIME_S_2=5.0",
                "CX_SUBSET_TIME_S_3=1.0",
                "CX_SUBSET_TIME_S_4=5.0",
                "CX_SUBSET_TIME_S_5=1.0",
                "CX_SUBSET_TIME_S_6=5.0",
                "CX_SUBSET_TIME_S_7=1.0",
                "CX_MERGE_LINEAR_TIME_S=0.9",
                "CX_MERGE_TREE_TIME_S=0.7",
                "CX_MERGE_PYTHON_TIME_S=0.5",
                "CX_MERGE_SELECTED=python",
                "CX_MERGE_TIME_S=0.5",
                "CX_QUALITY_SSIM=0.999990000",
                "CX_QUALITY_WORST_TILE_SSIM=0.999900000",
                "CX_QUALITY_TILE_COUNT=64",
                "CX_QUALITY_PNG_MAE=0.000001000",
                "CX_QUALITY_PNG_MAXE=0.000010000",
                "CX_FANOUT_DIFF_RC=0",
                "CX_FANOUT_PROBE_OK",
            ]),
        }
        rec = fanout.parse_probe(stage, "scene_monkey.xml", 4096, 4, "CUDA", 8)
        self.assertEqual(rec["chunk_count"], 8)
        self.assertEqual(rec["chunk_sample_length"], 512)
        self.assertEqual(rec["static_chunk_wall_s"], 6.0)
        self.assertEqual(rec["dynamic_chunk_wall_s"], 7.0)
        self.assertEqual(rec["lpt_chunk_wall_s"], 6.0)
        self.assertEqual(rec["merge_selected"], "python")
        self.assertEqual(rec["merge_linear_time_s"], 0.9)
        self.assertEqual(rec["merge_tree_time_s"], 0.7)
        self.assertEqual(rec["merge_python_time_s"], 0.5)
        self.assertEqual(rec["ssim"], 0.99999)
        self.assertEqual(rec["worst_tile_ssim"], 0.9999)

    def test_parse_probe_detects_scheduler_gain_for_imbalanced_chunks(self):
        stage = {
            "ok": True,
            "elapsed_s": 10.0,
            "out_tail": "\n".join([
                "CX_FULL_TIME_S=40.0",
                "CX_CHUNK_COUNT=8",
                "CX_SUBSET_TIME_S_0=8.0",
                "CX_SUBSET_TIME_S_1=8.0",
                "CX_SUBSET_TIME_S_2=1.0",
                "CX_SUBSET_TIME_S_3=1.0",
                "CX_SUBSET_TIME_S_4=8.0",
                "CX_SUBSET_TIME_S_5=8.0",
                "CX_SUBSET_TIME_S_6=1.0",
                "CX_SUBSET_TIME_S_7=1.0",
                "CX_MERGE_TIME_S=1.0",
                "CX_FANOUT_DIFF_RC=0",
                "CX_FANOUT_PROBE_OK",
            ]),
        }
        rec = fanout.parse_probe(stage, "scene_cube_volume.xml", 4096, 4, "CUDA", 8)
        self.assertEqual(rec["static_chunk_wall_s"], 16.0)
        self.assertEqual(rec["dynamic_chunk_wall_s"], 9.0)
        self.assertGreater(rec["chunk_scheduler_gain_vs_static"], 1.5)

    def test_parse_probe_reports_actual_parallel_wall(self):
        stage = {
            "ok": True,
            "elapsed_s": 10.0,
            "out_tail": "\n".join([
                "CX_FULL_TIME_S=20.0",
                "CX_SUBSET_EXECUTION_MODE=parallel",
                "CX_PARALLEL_SLOTS=3",
                "CX_SUBSET_RETRIES=1",
                "CX_CHUNK_COUNT=8",
                "CX_SUBSET_TIME_S_0=2.0",
                "CX_SUBSET_TIME_S_1=2.1",
                "CX_SUBSET_TIME_S_2=2.2",
                "CX_SUBSET_TIME_S_3=2.3",
                "CX_SUBSET_TIME_S_4=2.4",
                "CX_SUBSET_TIME_S_5=2.5",
                "CX_SUBSET_TIME_S_6=2.6",
                "CX_SUBSET_TIME_S_7=2.7",
                "CX_SUBSET_PHASE_WALL_S=5.5",
                "CX_MERGE_TIME_S=0.5",
                "CX_FANOUT_DIFF_RC=0",
                "CX_FANOUT_PROBE_OK",
            ]),
        }
        rec = fanout.parse_probe(stage, "scene_world_volume.xml", 1024, 4, "CUDA", 8)
        self.assertEqual(rec["execution_mode"], "parallel")
        self.assertEqual(rec["parallel_slots"], 3)
        self.assertEqual(rec["subset_retries"], 1)
        self.assertEqual(rec["subset_phase_wall_s"], 5.5)
        self.assertEqual(rec["actual_parallel_wall_s"], 6.0)
        self.assertEqual(rec["actual_speedup_vs_full"], 3.3333)

    def test_parse_probe_reports_batch_wall(self):
        stage = {
            "ok": True,
            "elapsed_s": 20.0,
            "out_tail": "\n".join([
                "CX_FULL_TIME_S=12.0",
                "CX_SUBSET_EXECUTION_MODE=batch",
                "CX_PARALLEL_SLOTS=4",
                "CX_SUBSET_RETRIES=1",
                "CX_CHUNK_COUNT=16",
                "CX_CHUNK_SAMPLE_LENGTH=256",
                "CX_BATCH_WORKER_TIME_S_0=4.0",
                "CX_BATCH_WORKER_TIME_S_1=4.2",
                "CX_SUBSET_TIME_S_0=4.0",
                "CX_SUBSET_TIME_S_1=4.2",
                "CX_SUBSET_PHASE_WALL_S=4.3",
                "CX_MERGE_TIME_S=0.7",
                "CX_FANOUT_DIFF_RC=0",
                "CX_BATCH_MANIFEST_OK workers=4 chunks=16",
                "CX_FANOUT_PROBE_OK",
            ]),
        }
        rec = fanout.parse_probe(stage, "scene_world_volume.xml", 4096, 4, "CUDA", 16)
        self.assertEqual(rec["execution_mode"], "batch")
        self.assertEqual(rec["chunk_count"], 16)
        self.assertEqual(rec["chunk_sample_length"], 256)
        self.assertEqual(rec["subset_phase_wall_s"], 4.3)
        self.assertEqual(rec["actual_parallel_wall_s"], 5.0)
        self.assertEqual(rec["actual_speedup_vs_full"], 2.4)

    def test_parse_probe_classifies_tiny_nonzero_diff_as_numeric(self):
        stage = {
            "ok": True,
            "elapsed_s": 10.0,
            "out_tail": "\n".join([
                "CX_FULL_TIME_S=5.38",
                "CX_SUBSET_TIME_S_0=1.22",
                "CX_SUBSET_TIME_S_1=1.28",
                "CX_MERGE_TIME_S=0.39",
                "Mean error = 2.10052e-06",
                "RMS error = 6.17292e-06",
                "Max error  = 5.370378494262695e-05",
                "CX_FANOUT_DIFF_RC=1",
                "CX_FANOUT_PROBE_OK",
            ]),
        }
        rec = fanout.parse_probe(stage, "scene_monkey.xml", 4096, 2, "CUDA")
        self.assertTrue(rec["ok"])
        self.assertFalse(rec["exact"])
        self.assertTrue(rec["numeric_equivalent"])
        self.assertEqual(rec["diff_class"], "numeric")

    def test_parse_probe_allows_small_scene_wide_mean_offset(self):
        stage = {
            "ok": True,
            "elapsed_s": 10.0,
            "out_tail": "\n".join([
                "CX_FULL_TIME_S=4.98",
                "CX_SUBSET_TIME_S_0=2.09",
                "CX_SUBSET_TIME_S_1=3.16",
                "CX_MERGE_TIME_S=0.26",
                "Mean error = 1.33593e-05",
                "RMS error = 1.60216e-05",
                "Max error  = 1.9282102584838867e-05",
                "CX_FANOUT_DIFF_RC=1",
                "CX_FANOUT_PROBE_OK",
            ]),
        }
        rec = fanout.parse_probe(stage, "scene_sphere_bump.xml", 4096, 4, "CUDA")
        self.assertTrue(rec["numeric_equivalent"])
        self.assertEqual(rec["diff_class"], "numeric")

    def test_parse_probe_rejects_localized_volume_drift(self):
        stage = {
            "ok": True,
            "elapsed_s": 10.0,
            "out_tail": "\n".join([
                "CX_FULL_TIME_S=8.38",
                "CX_SUBSET_TIME_S_0=2.20",
                "CX_SUBSET_TIME_S_1=1.86",
                "CX_MERGE_TIME_S=0.34",
                "Mean error = 1.53004e-05",
                "RMS error = 8.27917e-05",
                "Max error  = 0.009562134742736816",
                "CX_FANOUT_DIFF_RC=1",
                "CX_FANOUT_PROBE_OK",
            ]),
        }
        rec = fanout.parse_probe(stage, "scene_cube_volume.xml", 4096, 8, "CUDA")
        self.assertFalse(rec["numeric_equivalent"])
        self.assertEqual(rec["diff_class"], "drift")

    def test_parse_probe_keeps_large_nonzero_diff_as_drift(self):
        stage = {
            "ok": True,
            "elapsed_s": 10.0,
            "out_tail": "\n".join([
                "CX_FULL_TIME_S=8.0",
                "CX_SUBSET_TIME_S_0=4.1",
                "CX_SUBSET_TIME_S_1=4.0",
                "CX_MERGE_TIME_S=0.3",
                "Mean error = 0.000348262",
                "RMS error = 0.00140771",
                "Max error  = 0.03678283095359802",
                "CX_FANOUT_DIFF_RC=1",
                "CX_FANOUT_PROBE_OK",
            ]),
        }
        rec = fanout.parse_probe(stage, "scene_monkey.xml", 4096, 2, "CUDA")
        self.assertTrue(rec["ok"])
        self.assertFalse(rec["exact"])
        self.assertFalse(rec["numeric_equivalent"])
        self.assertEqual(rec["diff_class"], "drift")
        self.assertEqual(rec["diff_rc"], 1)
        self.assertEqual(rec["rms_error"], 0.00140771)

    def test_skip_build_dry_run_uses_runtime_smokes(self):
        out = subprocess.check_output([
            sys.executable,
            os.path.join(HERE, "run_cycles_sample_fanout_matrix.py"),
            "--dry-run",
            "--skip-build",
            "--remote-root", "/opt/cx-cycles",
            "--samples", "64",
            "--fanouts", "2,4",
            "--device", "CUDA",
            "--disable-adaptive-sampling",
            "--chunks-per-worker", "2",
            "--merge-mode", "auto",
            "--execution-mode", "parallel",
            "--gpu-tier", "hopper",
        ], text=True)
        self.assertIn('"skip_build": true', out)
        self.assertIn('"disable_adaptive_sampling": true', out)
        self.assertIn('"chunks_per_worker": 2', out)
        self.assertIn('"merge_mode": "auto"', out)
        self.assertIn('"execution_mode": "parallel"', out)
        self.assertIn('"gpu_tier": "hopper"', out)
        self.assertIn("/opt/cx-cycles/install/cycles --device CUDA", out)
        self.assertIn('"chunk_count": 4', out)
        self.assertIn('"chunk_count": 8', out)
        self.assertIn('"scenes": [', out)
        self.assertIn('"fanouts": [', out)
        self.assertNotIn('"name": "clone"', out)
        self.assertNotIn('"name": "build"', out)

    def test_ada_tier_dry_run_is_recorded(self):
        out = subprocess.check_output([
            sys.executable,
            os.path.join(HERE, "run_cycles_sample_fanout_matrix.py"),
            "--dry-run",
            "--gpu-tier", "ada",
            "--remote-root", "/opt/cx-cycles",
            "--samples", "64",
            "--fanouts", "2",
            "--device", "CUDA",
        ], text=True)
        self.assertIn('"gpu_tier": "ada"', out)

    def test_prebuilt_root_tar_dry_run_uses_runtime_smokes(self):
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tar:
            out = subprocess.check_output([
                sys.executable,
                os.path.join(HERE, "run_cycles_sample_fanout_matrix.py"),
                "--dry-run",
                "--prebuilt-root-tar", tar.name,
                "--remote-root", "/opt/cx-cycles",
                "--samples", "64",
                "--fanouts", "2",
                "--device", "CUDA",
            ], text=True)
        self.assertIn('"prebuilt_root_tar":', out)
        self.assertIn('"skip_build": false', out)
        self.assertIn('"name": "binary_smoke"', out)
        self.assertNotIn('"name": "clone"', out)
        self.assertNotIn('"name": "build"', out)

    def test_export_runtime_tar_dry_run_keeps_normal_build(self):
        out = subprocess.check_output([
            sys.executable,
            os.path.join(HERE, "run_cycles_sample_fanout_matrix.py"),
            "--dry-run",
            "--remote-root", "/opt/cx-cycles",
            "--export-runtime-tar", "/tmp/cx-runtime.tar.gz",
            "--samples", "64",
            "--fanouts", "2",
            "--device", "CUDA",
        ], text=True)
        self.assertIn('"export_runtime_tar": "/tmp/cx-runtime.tar.gz"', out)
        self.assertIn('"name": "clone"', out)
        self.assertIn('"name": "build"', out)

    def test_dry_run_accepts_multiple_scenes(self):
        out = subprocess.check_output([
            sys.executable,
            os.path.join(HERE, "run_cycles_sample_fanout_matrix.py"),
            "--dry-run",
            "--skip-build",
            "--remote-root", "/opt/cx-cycles",
            "--scene", "scene_monkey.xml,scene_caustics.xml",
            "--samples", "64",
            "--fanouts", "2",
            "--device", "CUDA",
        ], text=True)
        self.assertIn('"scene_monkey.xml"', out)
        self.assertIn('"scene_caustics.xml"', out)
        self.assertIn('"name": "fanout_scene_monkey.xml_64_2"', out)
        self.assertIn('"name": "fanout_scene_caustics.xml_64_2"', out)

    def test_validate_skip_build_pass_dry_run_adds_runtime_validation(self):
        out = subprocess.check_output([
            sys.executable,
            os.path.join(HERE, "run_cycles_sample_fanout_matrix.py"),
            "--dry-run",
            "--remote-root", "/opt/cx-cycles",
            "--samples", "64",
            "--fanouts", "2",
            "--device", "CUDA",
            "--validate-skip-build-pass",
            "--continue-on-probe-failure",
            "--parallel-slots", "8",
            "--subset-retries", "1",
        ], text=True)
        self.assertIn('"validate_skip_build_pass": true', out)
        self.assertIn('"continue_on_probe_failure": true', out)
        self.assertIn('"parallel_slots": 8', out)
        self.assertIn('"subset_retries": 1', out)
        self.assertIn('"skip_build_validation_stages": [', out)
        self.assertIn('"name": "binary_smoke"', out)
        self.assertIn('"name": "build"', out)

    def test_validate_skip_build_pass_rejects_primary_skip_build(self):
        proc = subprocess.run([
            sys.executable,
            os.path.join(HERE, "run_cycles_sample_fanout_matrix.py"),
            "--dry-run",
            "--skip-build",
            "--validate-skip-build-pass",
        ], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--validate-skip-build-pass requires", proc.stderr)

    def test_export_runtime_tar_rejects_skip_build(self):
        proc = subprocess.run([
            sys.executable,
            os.path.join(HERE, "run_cycles_sample_fanout_matrix.py"),
            "--dry-run",
            "--skip-build",
            "--export-runtime-tar", "/tmp/cx-runtime.tar.gz",
        ], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--export-runtime-tar requires", proc.stderr)


if __name__ == "__main__":
    unittest.main()
