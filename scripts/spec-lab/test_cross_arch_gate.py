#!/usr/bin/env python3
"""Local correctness checks for the cross-architecture consistency gate.

Everything here is LOCAL + SYNTHETIC — no GPU, no Blender, no cloud, no money.
On synthetic configs/matrices/images it proves:

  1. canonical_config / config_hash: type-normalized, key-order independent, any
     value change changes the hash, plumbing keys are rejected, equal seeds and
     bad frames refuse.
  2. Manifest build/validate: round-trips; a tampered config, wrong EXR sha256,
     wrong kind/version, or missing baselines all REFUSE before money is spent.
  3. render_kwargs_from_config: producer and replica build IDENTICAL kwargs from
     the same manifest config (structural cross-arch parity); the seed pair
     differs ONLY in seed (reuses the reference-consistency probe's tested
     assert); the determinism pair must be exactly identical.
  4. gate_report (stage 3, pure): status/gate_pass pending until the cross-arch
     half exists; the gate flips exactly at 0.95; within-noise vs systematic-bias
     classification; all four interpretation branches say what the numbers say;
     JSON round-trips with no bare NaN.
  5. The mirrored DELIVERY_WORST_TILE literal still equals the single source of
     truth (cx_integrated_speculation.DELIVERY_WORST_TILE).
  6. comparison_block through the REAL SSIM path: identical frames score 1.0
     everywhere; a planted noisy tile is the worst tile with the right identity.
  7. Driver plumbing: the shim compiles; the local default config is the tiny
     canonical config with require_gpu=True; the $-estimate is sane and labeled
     MODELED; importing the driver does NOT import runpod (the local-only wave
     structurally cannot touch the RunPod API); ledger read/report assembly
     (PENDING without stage 2, COMPLETE with it, config_hash mismatch refuses).
"""

import json
import math
import os
import sys
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
POD = os.path.join(HERE, "pod")
sys.path.insert(0, HERE)
sys.path.insert(0, POD)

import exp_cross_arch_gate as xag  # noqa: E402
import exp_render_stack as ers  # noqa: E402
import cx_integrated_speculation as integrated  # noqa: E402
import run_cross_arch_gate as driver  # noqa: E402

GRID = ers.GRADING_TILE_GRID


def base_params(**over):
    p = dict(scene="classroom", resolution="960x540", frame=1, nframes=2,
             ref_spp=512, bounces=12, cam_motion=1.0, seed_a=0, seed_b=12345)
    p.update(over)
    return p


def flat_matrix(values, grid=GRID):
    assert len(values) == grid * grid
    return [list(values[r * grid:(r + 1) * grid]) for r in range(grid)]


def synth_baselines(ss_worst=1.0, cs_worst=0.90, pixel_exact=True, seed_inert=False):
    b = {
        "same_seed": {"global_ssim": 1.0, "worst_tile_ssim": ss_worst,
                      "p5_tile_ssim": 1.0, "pixel_exact": pixel_exact},
        "cross_seed": {"global_ssim": 0.99, "worst_tile_ssim": cs_worst,
                       "p5_tile_ssim": 0.95},
    }
    if seed_inert:
        b["cross_seed"]["degenerate_seed_inert"] = True
    return b


def synth_cross(worst=0.96, global_ssim=0.995, p5=0.98):
    return {"global_ssim": global_ssim, "worst_tile_ssim": worst, "p5_tile_ssim": p5}


class TestCanonicalConfig(unittest.TestCase):
    def test_normalizes_types_and_case(self):
        cfg = xag.canonical_config(base_params(resolution="960X540", frame="1",
                                               ref_spp="512", cam_motion=1))
        self.assertEqual(cfg["resolution"], "960x540")
        self.assertIsInstance(cfg["frame"], int)
        self.assertIsInstance(cfg["cam_motion"], float)

    def test_missing_key_refuses(self):
        p = base_params()
        del p["ref_spp"]
        with self.assertRaises(RuntimeError):
            xag.canonical_config(p)

    def test_equal_seeds_refuse(self):
        with self.assertRaises(RuntimeError):
            xag.canonical_config(base_params(seed_a=7, seed_b=7))

    def test_frame_out_of_range_refuses(self):
        with self.assertRaises(RuntimeError):
            xag.canonical_config(base_params(frame=3, nframes=2))

    def test_bad_resolution_refuses(self):
        with self.assertRaises(RuntimeError):
            xag.canonical_config(base_params(resolution="960"))


class TestConfigHash(unittest.TestCase):
    def test_key_order_and_spelling_independent(self):
        cfg1 = xag.canonical_config(base_params())
        cfg2 = xag.canonical_config(dict(reversed(list(base_params(cam_motion=1).items()))))
        self.assertEqual(xag.config_hash(cfg1), xag.config_hash(cfg2))

    def test_any_value_change_changes_hash(self):
        h0 = xag.config_hash(xag.canonical_config(base_params()))
        for key, val in (("ref_spp", 256), ("seed_a", 1), ("resolution", "1920x1080"),
                         ("frame", 2), ("cam_motion", 0.5), ("bounces", 6)):
            h1 = xag.config_hash(xag.canonical_config(base_params(**{key: val})))
            self.assertNotEqual(h0, h1, f"hash did not change for {key}")

    def test_plumbing_keys_rejected(self):
        cfg = xag.canonical_config(base_params())
        cfg["device"] = "GPU"  # plumbing must never enter the hash
        with self.assertRaises(RuntimeError):
            xag.config_hash(cfg)
        cfg2 = xag.canonical_config(base_params())
        del cfg2["bounces"]
        with self.assertRaises(RuntimeError):
            xag.config_hash(cfg2)


class TestManifest(unittest.TestCase):
    def _manifest(self):
        cfg = xag.canonical_config(base_params())
        return xag.build_manifest(
            cfg, "ab" * 32,
            {"platform": "macOS", "machine": "arm64", "device": "GPU/METAL",
             "blender_version": "Blender 4.2.1 LTS"},
            synth_baselines())

    def test_roundtrip_validates(self):
        m = json.loads(json.dumps(self._manifest()))
        cfg = xag.validate_manifest(m, exr_sha256="ab" * 32)
        self.assertEqual(cfg["ref_spp"], 512)

    def test_tampered_config_refuses(self):
        m = self._manifest()
        m["config"]["ref_spp"] = 64  # edited after export -> hash mismatch
        with self.assertRaises(RuntimeError):
            xag.validate_manifest(m)

    def test_wrong_exr_sha_refuses(self):
        m = self._manifest()
        with self.assertRaises(RuntimeError):
            xag.validate_manifest(m, exr_sha256="cd" * 32)

    def test_wrong_kind_or_version_refuses(self):
        m = self._manifest()
        m["kind"] = "something_else"
        with self.assertRaises(RuntimeError):
            xag.validate_manifest(m)
        m2 = self._manifest()
        m2["version"] = 99
        with self.assertRaises(RuntimeError):
            xag.validate_manifest(m2)

    def test_missing_baselines_refuse(self):
        m = self._manifest()
        del m["baselines"]["cross_seed"]
        with self.assertRaises(RuntimeError):
            xag.validate_manifest(m)

    def test_no_sha_check_when_exr_sha_none(self):
        # Config validation still runs; the EXR check is caller-supplied.
        cfg = xag.validate_manifest(self._manifest(), exr_sha256=None)
        self.assertEqual(cfg["seed_b"], 12345)


class TestRenderKwargsParity(unittest.TestCase):
    def test_producer_and_replica_build_identical_kwargs(self):
        cfg = xag.canonical_config(base_params())
        mk = lambda: xag.render_kwargs_from_config(  # noqa: E731
            cfg, blend="/s.blend", seed=cfg["seed_a"], device_pref="GPU",
            timeout_s=3600, require_gpu=True)
        self.assertEqual(mk(), mk())
        self.assertEqual(xag.assert_identical_config(mk(), mk()), [])

    def test_seed_pair_differs_only_in_seed(self):
        import exp_reference_consistency_probe as xrc
        cfg = xag.canonical_config(base_params())
        kw = lambda seed: xag.render_kwargs_from_config(  # noqa: E731
            cfg, blend="/s.blend", seed=seed, device_pref="GPU",
            timeout_s=3600, require_gpu=True)
        self.assertEqual(xrc.assert_seed_only_diff(kw(cfg["seed_a"]), kw(cfg["seed_b"])),
                         ["seed"])

    def test_is_ref_forced(self):
        cfg = xag.canonical_config(base_params())
        kwargs = xag.render_kwargs_from_config(
            cfg, blend="/s.blend", seed=0, device_pref="GPU",
            timeout_s=3600, require_gpu=True)
        self.assertTrue(kwargs["is_ref"])
        self.assertEqual((kwargs["res_x"], kwargs["res_y"]), (960, 540))

    def test_determinism_pair_any_diff_refuses(self):
        cfg = xag.canonical_config(base_params())
        a = xag.render_kwargs_from_config(cfg, blend="/s.blend", seed=0,
                                          device_pref="GPU", timeout_s=3600,
                                          require_gpu=True)
        b = dict(a, spp=64)
        with self.assertRaises(RuntimeError):
            xag.assert_identical_config(a, b)


class TestDeliveryGateMirror(unittest.TestCase):
    def test_mirror_equals_source_of_truth(self):
        self.assertEqual(xag.DELIVERY_WORST_TILE, integrated.DELIVERY_WORST_TILE)


class TestGateReport(unittest.TestCase):
    def test_pending_when_no_cross_arch(self):
        r = xag.gate_report(synth_baselines())
        self.assertEqual(r["status"], "PENDING-CUDA-HALF")
        self.assertIsNone(r["gate_pass"])
        self.assertIsNone(r["cross_arch_worst_tile"])
        self.assertIn("PENDING", r["interpretation"])
        self.assertIn("same-arch half only", r["label"])
        # honest: nothing predicts the pending half
        self.assertIn("nothing here predicts it", r["interpretation"])

    def test_gate_flips_exactly_at_threshold(self):
        at = xag.gate_report(synth_baselines(), cross_arch=synth_cross(worst=0.95))
        below = xag.gate_report(synth_baselines(), cross_arch=synth_cross(worst=0.9499))
        self.assertTrue(at["gate_pass"])
        self.assertFalse(below["gate_pass"])
        self.assertAlmostEqual(at["cross_arch_gate_margin"], 0.0, places=6)

    def test_pass_within_noise(self):
        r = xag.gate_report(synth_baselines(cs_worst=0.90),
                            cross_arch=synth_cross(worst=0.96))
        self.assertTrue(r["gate_pass"])
        self.assertTrue(r["cross_arch_within_same_arch_noise"])
        self.assertFalse(r["systematic_arch_bias_suspected"])
        self.assertIn("GATE PASS, WITHIN NOISE", r["interpretation"])
        self.assertIn("both halves", r["label"])

    def test_pass_below_noise_floor_is_flagged(self):
        r = xag.gate_report(synth_baselines(cs_worst=0.99),
                            cross_arch=synth_cross(worst=0.96))
        self.assertTrue(r["gate_pass"])
        self.assertFalse(r["cross_arch_within_same_arch_noise"])
        self.assertTrue(r["systematic_arch_bias_suspected"])
        self.assertIn("BELOW THE SAME-ARCH NOISE FLOOR", r["interpretation"])

    def test_fail_when_same_arch_floor_also_fails_is_labeled_noise_dominated(self):
        r = xag.gate_report(synth_baselines(cs_worst=0.90),
                            cross_arch=synth_cross(worst=0.91))
        self.assertFalse(r["gate_pass"])
        self.assertFalse(r["same_arch_cross_seed_clears_gate"])
        self.assertIn("SO DOES SAME-ARCH RESEEDING", r["interpretation"])
        self.assertIn("gate_pass=false AT THIS CONFIG", r["interpretation"])

    def test_genuine_cross_arch_fail(self):
        r = xag.gate_report(synth_baselines(cs_worst=0.97),
                            cross_arch=synth_cross(worst=0.90))
        self.assertFalse(r["gate_pass"])
        self.assertTrue(r["same_arch_cross_seed_clears_gate"])
        self.assertTrue(r["systematic_arch_bias_suspected"])
        self.assertIn("NOT interchangeable", r["interpretation"])

    def test_verifier_floor_included_when_present(self):
        r = xag.gate_report(
            synth_baselines(), cross_arch=synth_cross(worst=0.96),
            verifier_cross_seed={"global_ssim": 0.99, "worst_tile_ssim": 0.91,
                                 "p5_tile_ssim": 0.94})
        self.assertAlmostEqual(r["verifier_cross_seed_worst_tile"], 0.91)
        self.assertIn("Verifier-side cross-seed floor", r["interpretation"])

    def test_json_roundtrip_no_nan(self):
        for r in (xag.gate_report(synth_baselines()),
                  xag.gate_report(synth_baselines(), cross_arch=synth_cross())):
            blob = json.dumps(r)
            self.assertNotIn("NaN", blob)
            json.loads(blob)


class TestSeedEffectClassifier(unittest.TestCase):
    """The MEASURED 2026-07-10 gotcha: on Blender 4.2.1, cycles.seed does not
    perturb the sample sequence — different seeds reproduce the same realization to
    float epsilon. The classifier must call that 'inert' and call real MC
    re-realization 'effective'."""

    def test_measured_local_values_classify_inert(self):
        # The real stage-1 numbers: same-seed max|d| 1.19e-6, cross-seed 1.43e-6.
        self.assertEqual(xag.classify_seed_effect(1.19e-6, 1.43e-6), "inert")

    def test_real_mc_noise_classifies_effective(self):
        # True re-realization at any practical spp is >= ~1e-2 raw max|d|.
        self.assertEqual(xag.classify_seed_effect(1.19e-6, 0.05), "effective")

    def test_bit_exact_same_seed_uses_absolute_epsilon(self):
        self.assertEqual(xag.classify_seed_effect(0.0, 5e-5), "inert")
        self.assertEqual(xag.classify_seed_effect(0.0, 2e-4), "effective")

    def test_ratio_guard(self):
        # cross-seed must exceed BOTH 10x the same-seed jitter and the epsilon.
        self.assertEqual(xag.classify_seed_effect(1e-3, 5e-3), "inert")
        self.assertEqual(xag.classify_seed_effect(1e-3, 2e-2), "effective")


class TestGateReportSeedInert(unittest.TestCase):
    def test_pending_inert_says_no_floor_exists(self):
        r = xag.gate_report(synth_baselines(cs_worst=1.0, seed_inert=True))
        self.assertEqual(r["status"], "PENDING-CUDA-HALF")
        self.assertTrue(r["same_arch_cross_seed_degenerate_seed_inert"])
        self.assertIn("MEASURED-INERT", r["interpretation"])
        self.assertIn("NO same-arch noise floor exists", r["interpretation"])

    def test_complete_inert_never_classifies_noise_or_bias(self):
        r = xag.gate_report(synth_baselines(cs_worst=1.0, seed_inert=True),
                            cross_arch=synth_cross(worst=0.96))
        self.assertTrue(r["gate_pass"])
        self.assertIsNone(r["cross_arch_within_same_arch_noise"])
        self.assertIsNone(r["systematic_arch_bias_suspected"])
        self.assertIn("MEASURED-INERT", r["interpretation"])
        self.assertIn("kernel-level", r["interpretation"])

    def test_complete_inert_fail_attributes_to_kernels(self):
        r = xag.gate_report(synth_baselines(cs_worst=1.0, seed_inert=True),
                            cross_arch=synth_cross(worst=0.90))
        self.assertFalse(r["gate_pass"])
        self.assertIn("cannot be Monte-Carlo reseeding noise", r["interpretation"])
        self.assertIn("NOT", r["interpretation"])

    def test_non_inert_behavior_unchanged(self):
        # The pre-existing four branches keep their exact semantics when the flag
        # is absent (regression guard for the fix).
        r = xag.gate_report(synth_baselines(cs_worst=0.90),
                            cross_arch=synth_cross(worst=0.96))
        self.assertTrue(r["cross_arch_within_same_arch_noise"])
        self.assertFalse(r["same_arch_cross_seed_degenerate_seed_inert"])


class TestDiffStats(unittest.TestCase):
    def test_identical_and_jittered(self):
        rng = np.random.default_rng(3)
        img = rng.uniform(0.0, 2.0, size=(32, 32, 3)).astype(np.float32)
        s = xag.diff_stats(img, img)
        self.assertTrue(s["pixel_exact"])
        self.assertEqual(s["max_abs_diff"], 0.0)
        jit = img + np.float32(1e-6)
        s2 = xag.diff_stats(img, jit)
        self.assertFalse(s2["pixel_exact"])
        self.assertLess(s2["max_abs_diff"], 1e-5)
        self.assertGreater(s2["max_abs_diff"], 0.0)


class TestComparisonBlock(unittest.TestCase):
    def test_identical_frames_score_one_everywhere(self):
        rng = np.random.default_rng(0)
        img = rng.uniform(0.0, 2.0, size=(96, 96, 3)).astype(np.float32)
        mat, g, w, p5 = (ers.per_tile_ssim_map(img, img),
                         *ers.compute_ssim_global_and_tiles(img, img))
        block = xag.comparison_block(mat, g, w, p5, GRID)
        self.assertEqual(block["worst_tile_ssim"], 1.0)
        self.assertEqual(block["global_ssim"], 1.0)
        self.assertEqual(len(block["tiles"]), GRID * GRID)
        self.assertTrue(all(t == 1.0 for t in block["tiles"] if t is not None))

    def test_planted_noisy_tile_is_worst_with_right_identity(self):
        rng = np.random.default_rng(1)
        base = rng.uniform(0.0, 2.0, size=(96, 96, 3)).astype(np.float32)
        noisy = base.copy()
        ty, tx = 96 // GRID, 96 // GRID
        gy, gx = 2, 5
        noisy[gy * ty:(gy + 1) * ty, gx * tx:(gx + 1) * tx] += rng.normal(
            0.0, 1.5, size=(ty, tx, 3)).astype(np.float32)
        mat = ers.per_tile_ssim_map(base, noisy)
        g, w, p5 = ers.compute_ssim_global_and_tiles(base, noisy)
        block = xag.comparison_block(mat, g, w, p5, GRID)
        self.assertEqual(block["worst_tile"]["tile"], [gy, gx])
        self.assertAlmostEqual(block["worst_tile"]["ssim"], block["worst_tile_ssim"],
                               places=6)
        self.assertLess(block["worst_tile_ssim"], 0.9)

    def test_nan_tiles_stay_null_and_size_enforced(self):
        vals = [1.0] * (GRID * GRID)
        vals[3] = float("nan")
        block = xag.comparison_block(flat_matrix(vals), 1.0, 1.0, 1.0, GRID)
        self.assertIsNone(block["tiles"][3])
        self.assertEqual(block["n_valid_tiles"], GRID * GRID - 1)
        with self.assertRaises(ValueError):
            xag.comparison_block([[1.0]], 1.0, 1.0, 1.0, GRID)


class TestBaselinesFromMetrics(unittest.TestCase):
    def test_extracts_and_defaults_pixel_exact(self):
        metrics = {"comparisons": synth_baselines(pixel_exact=None),
                   "same_seed_pixel_exact": True}
        b = xag.baselines_from_metrics(metrics)
        self.assertTrue(b["same_seed"]["pixel_exact"])
        self.assertEqual(b["cross_seed"]["worst_tile_ssim"], 0.90)

    def test_missing_blocks_refuse(self):
        with self.assertRaises(RuntimeError):
            xag.baselines_from_metrics({"comparisons": {"same_seed": {}}})


class TestDriverPlumbing(unittest.TestCase):
    def test_shim_compiles(self):
        compile(driver.SHIM_SOURCE, "cx_cross_arch_shim.py", "exec")

    def test_driver_import_does_not_import_runpod(self):
        # The local-only wave must be STRUCTURALLY unable to touch the RunPod API:
        # importing the driver (already imported at the top of this test module)
        # must not have pulled in runpod. Also assert the source only imports it
        # inside cmd_cuda.
        self.assertNotIn("runpod", sys.modules,
                         "importing run_cross_arch_gate imported runpod — the "
                         "local-only guarantee is broken")
        src = open(os.path.join(HERE, "run_cross_arch_gate.py")).read()
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import runpod", "from runpod")):
                self.assertTrue(line.startswith("    "),
                                f"top-level runpod import found: {line!r}")

    def test_local_default_config_is_the_tiny_canonical_one(self):
        args = driver.parse_args(["local"])
        cfg = driver.build_local_config(args)
        self.assertEqual(cfg["mode"], "self_consistency")
        self.assertEqual(cfg["resolution"], "960x540")
        self.assertEqual(cfg["ref_spp"], 512)
        self.assertEqual((cfg["seed_a"], cfg["seed_b"]), (0, 12345))
        self.assertTrue(cfg["require_gpu"])   # never a silent CPU baseline
        self.assertTrue(cfg["export"])
        # the canonical subset must hash cleanly
        canon = xag.canonical_config(cfg)
        self.assertTrue(xag.config_hash(canon))

    def test_estimate_is_sane_and_labeled(self):
        est = driver.estimate_cuda_cost()
        self.assertIn("MODELED", est["label"])
        self.assertGreater(est["usd_low"], 0.0)
        self.assertLess(est["usd_low"], est["usd_high"])
        self.assertLess(est["usd_high"], 2.0)   # a tiny replica must stay ~$0.50-class
        self.assertIn("basis", est)
        self.assertEqual(est["n_renders"], 2)

    def test_parse_last_json_line_reused(self):
        out = "noise\n{'not': json}\n" + json.dumps({"ok": 1}) + "\n"
        self.assertEqual(driver.lma.parse_last_json_line(out), {"ok": 1})


class TestReportAssembly(unittest.TestCase):
    def _local_row(self, chash="deadbeef"):
        return {
            "event": driver.EVENT_LOCAL,
            "evidence": driver.EVIDENCE_LOCAL,
            "ts": "2026-07-10T12:00:00-0400",
            "row": {
                "config": xag.canonical_config(base_params()),
                "config_hash": chash,
                "comparisons": synth_baselines(),
                "same_seed_pixel_exact": True,
                "device": "GPU/METAL",
                "producer": {"machine": "arm64"},
            },
        }

    def _cuda_row(self, chash="deadbeef", worst=0.96):
        return {
            "event": driver.EVENT_CUDA_RESULT,
            "metrics": {
                "config_hash": chash,
                "cross_arch": synth_cross(worst=worst),
                "verifier_cross_seed": None,
                "device": "GPU/OPTIX",
                "replica": {"machine": "x86_64"},
                "walls_s": {"replica_a": 20.0},
            },
        }

    def test_no_rows_pending_local(self):
        r = driver.assemble_report([])
        self.assertEqual(r["status"], "PENDING-LOCAL-HALF")
        self.assertIsNone(r["gate_pass"])

    def test_local_only_pending_cuda(self):
        r = driver.assemble_report([self._local_row()])
        self.assertEqual(r["status"], "PENDING-CUDA-HALF")
        self.assertIsNone(r["gate_pass"])
        self.assertEqual(r["config_hash"], "deadbeef")
        self.assertEqual(r["local_half"]["evidence"], driver.EVIDENCE_LOCAL)
        self.assertIsNone(r["cuda_half"])

    def test_both_halves_complete(self):
        r = driver.assemble_report([self._local_row(), self._cuda_row(worst=0.96)])
        self.assertEqual(r["status"], "COMPLETE")
        self.assertTrue(r["gate_pass"])
        self.assertEqual(r["cuda_half"]["device"], "GPU/OPTIX")

    def test_config_hash_mismatch_refuses(self):
        with self.assertRaises(RuntimeError):
            driver.assemble_report(
                [self._local_row(chash="aaaa"), self._cuda_row(chash="bbbb")])

    def test_latest_success_skips_error_rows(self):
        rows = [self._local_row(),
                {"event": driver.EVENT_CUDA_RESULT, "metrics": {"error": "boom"}}]
        self.assertIsNone(driver.latest_success(rows, driver.EVENT_CUDA_RESULT,
                                                "metrics"))
        r = driver.assemble_report(rows)
        self.assertEqual(r["status"], "PENDING-CUDA-HALF")

    def test_export_dir_resolution(self):
        row = self._local_row()
        row["row"]["export_dir"] = "/tmp/x/deadbeef"
        self.assertEqual(driver.resolve_export_dir(None, [row]), "/tmp/x/deadbeef")
        self.assertEqual(driver.resolve_export_dir("/explicit", []), "/explicit")
        with self.assertRaises(RuntimeError):
            driver.resolve_export_dir(None, [])


class TestSelfConsistencyMetricsShape(unittest.TestCase):
    """The pure gate_report embedded in stage-1 metrics must be pending-shaped, and
    baselines_from_metrics must round-trip what run_self_consistency assembles."""

    def test_stage1_gate_report_is_pending(self):
        baselines = synth_baselines()
        r = xag.gate_report(baselines, cross_arch=None)
        self.assertEqual(r["status"], "PENDING-CUDA-HALF")
        metrics = {"comparisons": baselines, "same_seed_pixel_exact": True,
                   "gate_report": r}
        b = xag.baselines_from_metrics(metrics)
        r2 = xag.gate_report(b)
        self.assertEqual(r2["status"], "PENDING-CUDA-HALF")
        self.assertEqual(r2["same_arch_cross_seed_worst_tile"],
                         r["same_arch_cross_seed_worst_tile"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
