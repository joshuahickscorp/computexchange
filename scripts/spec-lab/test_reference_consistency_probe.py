#!/usr/bin/env python3
"""Local correctness checks for the reference self-consistency probe.

Everything here is LOCAL + SYNTHETIC — no GPU, no Blender, no cloud, no money.
On synthetic tile arrays and images it proves:

  1. assert_seed_only_diff: passes when A and B differ ONLY in seed; raises when
     any other key differs, and raises when the two seeds are equal (identical
     renders would score a trivial SSIM 1.0).
  2. interpret_gate: the boolean flips exactly at the 0.95 gate, both interpretation
     strings are honest about their direction, and the mirrored DELIVERY_WORST_TILE
     literal still equals the single source of truth
     (cx_integrated_speculation.DELIVERY_WORST_TILE).
  3. The self-consistency SSIM path uses EXACTLY the grading-grid tiling: two identical
     reference frames score SSIM 1.0 everywhere; a planted noise-like tile is the worst
     self-consistency tile; per_tile_ssim_map and compute_ssim_global_and_tiles agree on
     the worst tile's value.
  4. probe_metrics JSON contract: all required keys, the full 64-tile array, NaN -> null,
     gate_reachable matches the measured worst tile, the worst-consistency tile identity
     is the argmin-SSIM tile, and json.dumps round-trips with no bare NaN token.
  5. End-to-end constructed case through the REAL SSIM path: a below-gate worst tile
     produces gate_reachable=False and a not-converged interpretation; an all-clean pair
     produces gate_reachable=True.
"""

import json
import os
import sys
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
POD = os.path.join(HERE, "pod")
sys.path.insert(0, HERE)
sys.path.insert(0, POD)

import exp_reference_consistency_probe as probe  # noqa: E402
import exp_render_stack as ers  # noqa: E402
import cx_integrated_speculation as integrated  # noqa: E402

GRID = ers.GRADING_TILE_GRID
NAN = float("nan")


def flat_matrix(values, grid=GRID):
    """Row-major 64-list -> [grid][grid] matrix."""
    assert len(values) == grid * grid
    return [list(values[r * grid:(r + 1) * grid]) for r in range(grid)]


class TestSeedOnlyDiff(unittest.TestCase):
    def _cfg(self, **over):
        base = dict(blend="/x.blend", res_x=3840, res_y=2160, spp=4096, is_ref=True,
                    frame=1, nframes=4, cam_motion=1.0, bounces=12, device_pref="GPU",
                    timeout_s=3600, require_gpu=True, seed=0)
        base.update(over)
        return base

    def test_seed_only_diff_passes(self):
        a = self._cfg(seed=0)
        b = self._cfg(seed=12345)
        self.assertEqual(probe.assert_seed_only_diff(a, b), ["seed"])

    def test_other_key_diff_raises(self):
        a = self._cfg(seed=0)
        b = self._cfg(seed=12345, spp=2048)   # a forbidden second difference
        with self.assertRaises(RuntimeError):
            probe.assert_seed_only_diff(a, b)

    def test_equal_seeds_raise(self):
        a = self._cfg(seed=7)
        b = self._cfg(seed=7)
        with self.assertRaises(RuntimeError):
            probe.assert_seed_only_diff(a, b)

    def test_identical_configs_raise(self):
        # No differing keys at all -> not "seed only" -> raise.
        a = self._cfg(seed=3)
        with self.assertRaises(RuntimeError):
            probe.assert_seed_only_diff(a, dict(a))


class TestInterpretGate(unittest.TestCase):
    def test_gate_literal_matches_single_source_of_truth(self):
        # The pod runner mirrors the gate literal because cx_integrated_speculation is
        # not scp'd into pod/; this asserts the mirror never drifts.
        self.assertEqual(probe.DELIVERY_WORST_TILE, integrated.DELIVERY_WORST_TILE)
        self.assertEqual(probe.DELIVERY_WORST_TILE, 0.95)

    def test_above_gate_is_reachable(self):
        reachable, text = probe.interpret_gate(0.97)
        self.assertTrue(reachable)
        self.assertIn("SELF-CONSISTENT", text)

    def test_at_gate_is_reachable(self):
        reachable, _ = probe.interpret_gate(0.95)
        self.assertTrue(reachable)   # >= is inclusive

    def test_below_gate_is_unreachable(self):
        reachable, text = probe.interpret_gate(0.914)
        self.assertFalse(reachable)
        self.assertIn("UNREACHABLE BY CONSTRUCTION", text)
        self.assertIn("NOT CONVERGED", text)

    def test_boolean_is_native_bool(self):
        reachable, _ = probe.interpret_gate(0.914)
        self.assertIsInstance(reachable, bool)


class TestSelfConsistencyTiling(unittest.TestCase):
    def _base_image(self, h=96, w=128, seed=7):
        rng = np.random.default_rng(seed)
        img = 0.5 + 0.1 * rng.standard_normal((h, w, 3)).astype(np.float32)
        return np.clip(img, 0.01, None)

    def test_identical_frames_score_one_everywhere(self):
        img = self._base_image()
        mat = ers.per_tile_ssim_map(img, img)
        self.assertEqual(mat.shape, (GRID, GRID))
        finite = mat[np.isfinite(mat)]
        self.assertTrue(finite.size > 0)
        self.assertTrue(np.allclose(finite, 1.0, atol=1e-9))

    def test_planted_noise_tile_is_worst_consistency(self):
        # A tile where A and B disagree (independent noise) must be the argmin SSIM tile.
        a = self._base_image(seed=1)
        b = a.copy()
        rects = ers._tile_rects(a.shape[0], a.shape[1], GRID)
        _gy, _gx, y0, y1, x0, x1 = rects[3 * GRID + 5]   # tile (3, 5)
        rng = np.random.default_rng(99)
        b[y0:y1, x0:x1] = np.clip(
            0.5 + 0.3 * rng.standard_normal(b[y0:y1, x0:x1].shape), 0.01, None)
        mat = ers.per_tile_ssim_map(a, b)
        self.assertEqual(np.nanargmin(mat), 3 * GRID + 5)

    def test_worst_tile_value_agrees_across_functions(self):
        a = self._base_image(seed=2)
        b = a.copy()
        rects = ers._tile_rects(a.shape[0], a.shape[1], GRID)
        _gy, _gx, y0, y1, x0, x1 = rects[10]
        rng = np.random.default_rng(5)
        b[y0:y1, x0:x1] = np.clip(
            0.5 + 0.3 * rng.standard_normal(b[y0:y1, x0:x1].shape), 0.01, None)
        mat = ers.per_tile_ssim_map(a, b)
        _g, worst, _p5 = ers.compute_ssim_global_and_tiles(a, b)
        # compute_ssim_global_and_tiles' worst == the argmin tile of per_tile_ssim_map
        self.assertAlmostEqual(float(np.nanmin(mat)), worst, places=10)


class TestProbeMetricsContract(unittest.TestCase):
    REQUIRED_KEYS = [
        "probe", "label", "hypothesis", "grid", "n_tiles", "n_valid_tiles",
        "delivery_worst_tile_gate", "global_ref_vs_ref", "worst_tile_ref_vs_ref",
        "p5_tile_ref_vs_ref", "ref_vs_ref_tiles", "worst_self_consistency_tile",
        "gate_reachable", "interpretation", "seed_a", "seed_b",
        "wall_ref_a_s", "wall_ref_b_s", "device", "modeled", "note",
    ]

    def _metrics(self, ssim_vals=None, worst=None):
        vals = ssim_vals if ssim_vals is not None else [0.99 - 0.0001 * i for i in range(64)]
        mat = flat_matrix(vals)
        finite = [v for v in vals if v == v]  # drop NaN
        w = worst if worst is not None else min(finite)
        g = sum(finite) / len(finite)
        p5 = float(np.percentile(np.asarray(finite), 5))
        return probe.probe_metrics(
            mat, g, w, p5, grid=GRID, seeds=(0, 12345),
            walls={"ref_a": 1130.2, "ref_b": 1131.7}, device="GPU/OPTIX",
            params_echo={"scene": "classroom", "ref_spp": 4096},
            note="synthetic contract test",
        )

    def test_required_keys_and_shapes(self):
        m = self._metrics()
        for key in self.REQUIRED_KEYS:
            self.assertIn(key, m, key)
        self.assertEqual(m["grid"], GRID)
        self.assertEqual(m["n_tiles"], 64)
        self.assertEqual(m["n_valid_tiles"], 64)
        self.assertEqual(len(m["ref_vs_ref_tiles"]), 64)
        self.assertEqual(m["label"], "MEASURED")
        self.assertIs(m["modeled"], False)
        self.assertEqual(m["delivery_worst_tile_gate"], 0.95)
        # params echo merged in
        self.assertEqual(m["scene"], "classroom")
        self.assertEqual(m["ref_spp"], 4096)
        self.assertEqual(m["seed_a"], 0)
        self.assertEqual(m["seed_b"], 12345)

    def test_gate_reachable_true_when_worst_above_gate(self):
        # worst tile 0.986 (all values >= 0.986) -> reachable.
        vals = [0.986 + 0.0001 * i for i in range(64)]
        m = self._metrics(ssim_vals=vals)
        self.assertTrue(m["gate_reachable"])
        self.assertIn("SELF-CONSISTENT", m["interpretation"])

    def test_gate_reachable_false_when_worst_below_gate(self):
        # Plant a below-gate worst tile (the whole point of the probe).
        vals = [0.99] * 64
        vals[42] = 0.914
        m = self._metrics(ssim_vals=vals, worst=0.914)
        self.assertFalse(m["gate_reachable"])
        self.assertIn("UNREACHABLE BY CONSTRUCTION", m["interpretation"])
        # The worst-consistency tile identity is the argmin-SSIM tile (index 42).
        self.assertEqual(m["worst_self_consistency_tile"]["tile"], [42 // GRID, 42 % GRID])
        self.assertAlmostEqual(m["worst_self_consistency_tile"]["ssim"], 0.914, places=6)

    def test_nan_tiles_become_null_and_shrink_valid_count(self):
        vals = [0.99 - 0.0001 * i for i in range(64)]
        vals[5] = NAN
        m = self._metrics(ssim_vals=vals)
        self.assertIsNone(m["ref_vs_ref_tiles"][5])
        self.assertEqual(m["n_valid_tiles"], 63)

    def test_worst_consistency_tile_is_argmin_ssim(self):
        vals = [0.99] * 64
        vals[17] = 0.80   # the single lowest-SSIM (worst-consistency) tile
        m = self._metrics(ssim_vals=vals, worst=0.80)
        self.assertEqual(m["worst_self_consistency_tile"]["tile"], [17 // GRID, 17 % GRID])

    def test_json_round_trip_no_nan_token(self):
        vals = [0.99 - 0.0001 * i for i in range(64)]
        vals[0] = NAN
        m = self._metrics(ssim_vals=vals)
        s = json.dumps(m)
        self.assertNotIn("NaN", s)
        self.assertEqual(json.loads(s)["worst_tile_ref_vs_ref"], m["worst_tile_ref_vs_ref"])

    def test_wrong_tile_count_raises(self):
        with self.assertRaises(ValueError):
            probe.probe_metrics(
                [[0.9]], 0.9, 0.9, 0.9, grid=GRID, seeds=(0, 1),
                walls={"ref_a": 1, "ref_b": 1}, device="GPU",
                params_echo={}, note="")


class TestEndToEndConstructed(unittest.TestCase):
    def _base(self, h=96, w=128, seed=3):
        rng = np.random.default_rng(seed)
        return np.clip(0.5 + 0.1 * rng.standard_normal((h, w, 3)), 0.01, None
                       ).astype(np.float32)

    def test_below_gate_tile_flags_unreachable(self):
        """Two 'reference' frames that agree everywhere except one heavily-noisy tile
        (the frame-edge convergence-failure analogue) -> a below-gate worst tile ->
        gate_reachable=False through the REAL SSIM path."""
        a = self._base(seed=3)
        b = a.copy()
        rects = ers._tile_rects(96, 128, GRID)
        _gy, _gx, y0, y1, x0, x1 = rects[0]   # a corner tile
        rng = np.random.default_rng(21)
        # strong independent noise in this one tile drives its SSIM well below 0.95
        b[y0:y1, x0:x1] = np.clip(
            0.5 + 0.4 * rng.standard_normal(b[y0:y1, x0:x1].shape), 0.01, None)
        mat = ers.per_tile_ssim_map(a, b)
        g, worst, p5 = ers.compute_ssim_global_and_tiles(a, b)
        m = probe.probe_metrics(
            mat, g, worst, p5, grid=GRID, seeds=(0, 12345),
            walls={"ref_a": 1.0, "ref_b": 1.0}, device="GPU",
            params_echo={}, note="e2e")
        self.assertLess(worst, 0.95)
        self.assertFalse(m["gate_reachable"])
        self.assertEqual(m["worst_self_consistency_tile"]["tile"], [0, 0])

    def test_identical_frames_flag_reachable(self):
        a = self._base(seed=4)
        mat = ers.per_tile_ssim_map(a, a)
        g, worst, p5 = ers.compute_ssim_global_and_tiles(a, a)
        m = probe.probe_metrics(
            mat, g, worst, p5, grid=GRID, seeds=(0, 12345),
            walls={"ref_a": 1.0, "ref_b": 1.0}, device="GPU",
            params_echo={}, note="e2e")
        self.assertTrue(m["gate_reachable"])
        self.assertAlmostEqual(m["worst_tile_ref_vs_ref"], 1.0, places=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
