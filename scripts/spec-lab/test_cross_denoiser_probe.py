#!/usr/bin/env python3
"""Local correctness checks for the cross-denoiser bias-detector probe.

Everything here is LOCAL + SYNTHETIC — no GPU, no Blender, no cloud, no money.
On synthetic tile arrays and images it proves:

  1. topk_indices: descending order, deterministic tie-break (lower index
     first), None/NaN skipped, k clamped.
  2. recall_at_k: a planted BIAS tile ranked first by D is found (recall@1=1);
     the RUN 4 failure mode (D ranks variance tiles, truth is elsewhere)
     scores exactly 0.0; partial overlap; k>n clamps via k_eff; k<1 raises;
     nothing-rankable returns None (never a fabricated 0 or 1).
  3. spearman_rank_corr: +1 / -1 on monotone data, tie-averaged ranks match a
     hand-computed value, None on constant input or <2 pairs, NaN pairs
     dropped, length mismatch raises.
  4. tile_dissimilarity uses EXACTLY the grading-grid tiling: identical images
     score D==0 everywhere; a planted constant-offset (bias-like) tile is the
     argmax of D; grid shape matches _tile_rects.
  5. End-to-end constructed bias case through the REAL SSIM path:
     oidn = base + bias patch in one tile, optix = base, ref = base
     -> top tile by D == worst tile by E_oidn -> recall@1 == 1.0.
  6. probe_metrics JSON contract: all required keys, full 64-tile arrays,
     NaN -> null, worst-tile identities correct, json.dumps round-trips.
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

import exp_cross_denoiser_probe as probe  # noqa: E402
import exp_render_stack as ers  # noqa: E402

GRID = ers.GRADING_TILE_GRID
NAN = float("nan")


def flat_matrix(values, grid=GRID):
    """Row-major 64-list -> [grid][grid] matrix."""
    assert len(values) == grid * grid
    return [list(values[r * grid:(r + 1) * grid]) for r in range(grid)]


class TestTopK(unittest.TestCase):
    def test_descending_with_deterministic_tie_break(self):
        vals = [0.1, 0.9, 0.9, 0.5]
        # ties (indices 1 and 2 at 0.9) break to the LOWER index first
        self.assertEqual(probe.topk_indices(vals, 3), [1, 2, 3])

    def test_skips_none_and_nan(self):
        vals = [None, 0.2, NAN, 0.8, float("inf")]
        self.assertEqual(probe.topk_indices(vals, 10), [3, 1])

    def test_k_clamped_and_zero(self):
        self.assertEqual(probe.topk_indices([1.0, 2.0], 5), [1, 0])
        self.assertEqual(probe.topk_indices([1.0, 2.0], 0), [])

    def test_negative_k_raises(self):
        with self.assertRaises(ValueError):
            probe.topk_indices([1.0], -1)


class TestRecallAtK(unittest.TestCase):
    def test_bias_tile_ranked_first_is_found(self):
        # A constructed bias case: tile 10 dominates BOTH the selector signal D
        # and the true error E — recall@1 must be 1.0.
        D = [0.01] * 64
        E = [0.02] * 64
        D[10] = 0.5
        E[10] = 0.4
        r, k_eff = probe.recall_at_k(D, E, 1)
        self.assertEqual((r, k_eff), (1.0, 1))

    def test_run4_failure_mode_scores_zero(self):
        # The RUN 4 pathology, synthetically: D ranks variance tiles {0..3}
        # highest while the true-worst tiles are {60..63} -> recall@4 == 0.0.
        D = [0.001 * (i + 1) for i in range(64)]
        E = [0.001 * (i + 1) for i in range(64)]
        for i in range(4):
            D[i] = 0.9 - 0.01 * i     # selector's picks: 0,1,2,3
            E[60 + i] = 0.9 - 0.01 * i  # truth: 60,61,62,63
        r, k_eff = probe.recall_at_k(D, E, 4)
        self.assertEqual((r, k_eff), (0.0, 4))
        # ... and the bias-detector success case on the same shape: D agreeing
        # with truth scores 1.0.
        r2, _ = probe.recall_at_k(E, E, 4)
        self.assertEqual(r2, 1.0)

    def test_partial_overlap(self):
        D = [0.0] * 64
        E = [0.0] * 64
        for rank, i in enumerate((5, 6, 7, 8)):   # D's top-4
            D[i] = 1.0 - 0.01 * rank
        for rank, i in enumerate((5, 6, 40, 41)):  # truth's top-4 (2 shared)
            E[i] = 1.0 - 0.01 * rank
        r, k_eff = probe.recall_at_k(D, E, 4)
        self.assertEqual(k_eff, 4)
        self.assertAlmostEqual(r, 0.5)

    def test_k_larger_than_finite_clamps(self):
        D = [0.3, 0.1, None, NAN]
        E = [0.2, 0.4, 0.5, 0.6]
        # only indices {0,1} are finite in BOTH -> k_eff = 2; both top-2 sets
        # are {0,1} -> recall 1.0
        r, k_eff = probe.recall_at_k(D, E, 12)
        self.assertEqual((r, k_eff), (1.0, 2))

    def test_k_below_one_raises(self):
        with self.assertRaises(ValueError):
            probe.recall_at_k([1.0], [1.0], 0)

    def test_nothing_rankable_returns_none(self):
        r, k_eff = probe.recall_at_k([None, NAN], [1.0, 2.0], 1)
        self.assertIsNone(r)
        self.assertEqual(k_eff, 0)

    def test_length_mismatch_raises(self):
        with self.assertRaises(ValueError):
            probe.recall_at_k([1.0], [1.0, 2.0], 1)

    def test_ranking_restricted_to_shared_finite_universe(self):
        # index 3 is truth's global max but is NaN in D: both sides must rank
        # only the shared universe {0,1,2}, where both agree top-1 is index 2.
        D = [0.1, 0.2, 0.3, NAN]
        E = [0.1, 0.2, 0.3, 9.9]
        r, k_eff = probe.recall_at_k(D, E, 1)
        self.assertEqual((r, k_eff), (1.0, 1))


class TestSpearman(unittest.TestCase):
    def test_perfect_monotone(self):
        a = [1.0, 2.0, 3.0, 4.0]
        b = [10.0, 20.0, 30.0, 40.0]
        self.assertAlmostEqual(probe.spearman_rank_corr(a, b), 1.0)

    def test_perfect_inverse(self):
        a = [1.0, 2.0, 3.0, 4.0]
        b = [40.0, 30.0, 20.0, 10.0]
        self.assertAlmostEqual(probe.spearman_rank_corr(a, b), -1.0)

    def test_tie_average_ranks_hand_computed(self):
        # a=[1,1,2] -> ranks [1.5,1.5,3]; b=[1,2,3] -> ranks [1,2,3]
        # rho = 1.5 / sqrt(1.5 * 2) = 0.86602...
        rho = probe.spearman_rank_corr([1.0, 1.0, 2.0], [1.0, 2.0, 3.0])
        self.assertAlmostEqual(rho, 1.5 / math.sqrt(3.0), places=10)

    def test_constant_input_is_none(self):
        self.assertIsNone(probe.spearman_rank_corr([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]))

    def test_fewer_than_two_pairs_is_none(self):
        self.assertIsNone(probe.spearman_rank_corr([1.0], [2.0]))
        self.assertIsNone(probe.spearman_rank_corr([NAN, 1.0], [1.0, 2.0]))

    def test_nan_pairs_dropped(self):
        # dropping the NaN pair leaves a perfectly inverse relation
        rho = probe.spearman_rank_corr([1.0, NAN, 2.0, 3.0], [9.0, 5.0, 8.0, 7.0])
        self.assertAlmostEqual(rho, -1.0)

    def test_length_mismatch_raises(self):
        with self.assertRaises(ValueError):
            probe.spearman_rank_corr([1.0, 2.0], [1.0])


class TestTileDissimilarity(unittest.TestCase):
    def _base_image(self, h=96, w=128):
        rng = np.random.default_rng(7)
        # smooth-ish positive HDR-like field
        img = 0.5 + 0.1 * rng.standard_normal((h, w, 3)).astype(np.float32)
        return np.clip(img, 0.01, None)

    def test_identical_images_score_zero(self):
        img = self._base_image()
        D = probe.tile_dissimilarity(img, img)
        self.assertEqual(D.shape, (GRID, GRID))
        finite = D[np.isfinite(D)]
        self.assertTrue(finite.size > 0)
        self.assertTrue(np.allclose(finite, 0.0, atol=1e-9))

    def test_planted_bias_tile_is_argmax(self):
        # A constant offset in ONE tile is exactly a denoiser-bias-shaped
        # deviation (structure shift, not noise) — it must be D's argmax.
        img_a = self._base_image()
        img_b = img_a.copy()
        rects = ers._tile_rects(img_a.shape[0], img_a.shape[1], GRID)
        gy, gx, y0, y1, x0, x1 = rects[2 * GRID + 3]  # tile (2, 3)
        img_b[y0:y1, x0:x1] += 0.35
        D = probe.tile_dissimilarity(img_a, img_b)
        self.assertEqual(np.nanargmax(D), 2 * GRID + 3)
        self.assertGreater(D[2, 3], 0.01)

    def test_grid_matches_tile_rects(self):
        img = self._base_image(64, 64)
        D = probe.tile_dissimilarity(img, img)
        self.assertEqual(D.size, len(ers._tile_rects(64, 64, GRID)))


class TestConstructedBiasCaseEndToEnd(unittest.TestCase):
    def test_bias_tile_first_by_D_and_recall_one(self):
        """The probe's core promise, synthetically: OIDN carries a bias patch in
        one tile, OptiX does not, ref is the truth -> that tile is TOP by D AND
        worst by E_oidn, so recall@1 == 1.0 through the real SSIM path."""
        rng = np.random.default_rng(11)
        base = np.clip(0.5 + 0.1 * rng.standard_normal((96, 128, 3)), 0.01, None
                       ).astype(np.float32)
        oidn = base.copy()
        rects = ers._tile_rects(96, 128, GRID)
        gy, gx, y0, y1, x0, x1 = rects[5 * GRID + 1]  # tile (5, 1)
        oidn[y0:y1, x0:x1] += 0.4          # OIDN's systematic deviation
        optix = base.copy()                 # OptiX: different (here: no) bias
        ref = base                          # ground truth

        D = probe.tile_dissimilarity(oidn, optix)
        E_oidn = probe.tile_dissimilarity(oidn, ref)
        D_flat = probe.flatten_tiles(D)
        E_flat = probe.flatten_tiles(E_oidn)

        self.assertEqual(probe.topk_indices(D_flat, 1), [5 * GRID + 1])
        self.assertEqual(probe.topk_indices(E_flat, 1), [5 * GRID + 1])
        r, k_eff = probe.recall_at_k(D_flat, E_flat, 1)
        self.assertEqual((r, k_eff), (1.0, 1))

    def test_variance_blind_spot_reproduced(self):
        """The RUN 4 pathology through the real SSIM path: a SHARED deviation
        (same in oidn and optix — the shared-bias analogue) is INVISIBLE to D
        but dominates E_oidn -> recall@1 == 0.0. This is exactly the failure
        the cross-denoiser probe cannot fix if biases coincide — the honest
        negative outcome the cloud run can still return."""
        rng = np.random.default_rng(13)
        base = np.clip(0.5 + 0.1 * rng.standard_normal((96, 128, 3)), 0.01, None
                       ).astype(np.float32)
        rects = ers._tile_rects(96, 128, GRID)
        _gy, _gx, y0, y1, x0, x1 = rects[0]
        shared = base.copy()
        shared[y0:y1, x0:x1] += 0.4        # identical deviation in BOTH outputs
        # plant a tiny genuine disagreement elsewhere so D has a nonzero top
        oidn = shared.copy()
        _gy2, _gx2, y2, y3, x2, x3 = rects[63]
        oidn[y2:y3, x2:x3] += 0.02
        D = probe.tile_dissimilarity(oidn, shared)
        E_oidn = probe.tile_dissimilarity(oidn, base)
        r, _ = probe.recall_at_k(probe.flatten_tiles(D), probe.flatten_tiles(E_oidn), 1)
        self.assertEqual(r, 0.0)


class TestProbeMetricsContract(unittest.TestCase):
    REQUIRED_KEYS = [
        "probe", "label", "hypothesis", "grid", "n_tiles", "n_valid_tiles",
        "recall_at_1", "recall_at_4", "recall_at_12", "recall_k_eff",
        "spearman_D_vs_E_oidn", "spearman_D_vs_E_optix",
        "D_tiles", "E_oidn_tiles", "E_optix_tiles",
        "top_tile_by_D", "worst_tile_by_E_oidn", "worst_tile_by_E_optix",
        "top12_by_D", "top12_by_E_oidn",
        "wall_oidn_s", "wall_optix_s", "wall_ref_s",
        "device", "modeled", "note",
    ]

    def _metrics(self, D=None, E=None):
        vals_D = D if D is not None else [0.001 * (i + 1) for i in range(64)]
        vals_E = E if E is not None else [0.002 * (i + 1) for i in range(64)]
        return probe.probe_metrics(
            flat_matrix(vals_D), flat_matrix(vals_E), flat_matrix(vals_E),
            grid=GRID,
            params_echo={"scene": "synthetic", "draft_spp": 512},
            walls={"oidn": 203.1, "optix": 202.9, "ref": 1136.7},
            device="GPU/OPTIX",
            note="synthetic contract test",
        )

    def test_required_keys_and_shapes(self):
        m = self._metrics()
        for key in self.REQUIRED_KEYS:
            self.assertIn(key, m, key)
        self.assertEqual(m["grid"], GRID)
        self.assertEqual(m["n_tiles"], 64)
        self.assertEqual(m["n_valid_tiles"], 64)
        for key in ("D_tiles", "E_oidn_tiles", "E_optix_tiles"):
            self.assertEqual(len(m[key]), 64, key)
        self.assertEqual(len(m["top12_by_D"]), 12)
        self.assertEqual(len(m["top12_by_E_oidn"]), 12)
        self.assertEqual(m["label"], "MEASURED")
        self.assertIs(m["modeled"], False)
        # params echo merged in
        self.assertEqual(m["scene"], "synthetic")
        self.assertEqual(m["draft_spp"], 512)

    def test_monotone_agreement_scores_perfectly(self):
        m = self._metrics()  # D and E both strictly increasing -> same ranking
        self.assertEqual(m["recall_at_1"], 1.0)
        self.assertEqual(m["recall_at_4"], 1.0)
        self.assertEqual(m["recall_at_12"], 1.0)
        self.assertAlmostEqual(m["spearman_D_vs_E_oidn"], 1.0)
        self.assertEqual(m["recall_k_eff"], {"1": 1, "4": 4, "12": 12})
        self.assertEqual(m["top_tile_by_D"]["tile"], [7, 7])   # index 63
        self.assertEqual(m["worst_tile_by_E_oidn"]["tile"], [7, 7])

    def test_nan_tiles_become_null_and_shrink_valid_count(self):
        vals_D = [0.001 * (i + 1) for i in range(64)]
        vals_D[5] = NAN
        m = self._metrics(D=vals_D)
        self.assertIsNone(m["D_tiles"][5])
        self.assertEqual(m["n_valid_tiles"], 63)

    def test_json_round_trip(self):
        m = self._metrics()
        s = json.dumps(m)
        self.assertEqual(json.loads(s)["recall_at_12"], m["recall_at_12"])
        # strict JSON: no bare NaN tokens may appear even with NaN input tiles
        vals_D = [0.001 * (i + 1) for i in range(64)]
        vals_D[0] = NAN
        m2 = self._metrics(D=vals_D)
        self.assertNotIn("NaN", json.dumps(m2))

    def test_run4_shape_zero_recall_reported_honestly(self):
        # D top-4 disjoint from truth top-4 -> the contract must carry the 0.0,
        # not mask it.
        vals_D = [0.001 * (i + 1) for i in range(64)]
        vals_E = [0.001 * (i + 1) for i in range(64)]
        for i in range(4):
            vals_D[i] = 0.9 - 0.01 * i
            vals_E[60 + i] = 0.9 - 0.01 * i
        m = self._metrics(D=vals_D, E=vals_E)
        self.assertEqual(m["recall_at_4"], 0.0)

    def test_wrong_tile_count_raises(self):
        with self.assertRaises(ValueError):
            probe.probe_metrics(
                [[0.1]], flat_matrix([0.0] * 64), flat_matrix([0.0] * 64),
                grid=GRID, params_echo={}, walls={"oidn": 1, "optix": 1, "ref": 1},
                device="GPU", note="")


if __name__ == "__main__":
    unittest.main(verbosity=2)
