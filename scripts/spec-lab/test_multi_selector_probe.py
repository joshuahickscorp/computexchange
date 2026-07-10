#!/usr/bin/env python3
"""Local correctness checks for the exhaustive multi-selector reference-free probe.

Everything here is LOCAL + SYNTHETIC — no GPU, no Blender, no cloud, no money.
On constructed tile arrays and small synthetic images it proves:

  1. _selector_entry recall@k / Spearman vs E_oidn for EACH selector: a case where
     the SAMPLE-COUNT selector ranks the true-worst tile FIRST (recall@1 == 1.0);
     the RUN-4 failure mode (selector top-4 disjoint from truth) scores exactly 0.0;
     partial overlap; k>n clamps via k_eff; the shared finite tile universe is
     respected; monotone agreement scores perfectly.
  2. Unavailable-selector handling: available=false, every metric null (never a
     fabricated 0/1), the reason is carried, k_eff == 0.
  3. NaN tiles -> null in the "tiles" array and n_valid_tiles shrinks.
  4. The per-pixel selector FIELDS localize the planted region: denoiser-residual,
     content-gradient and normal-AOV-edge each put their argmax on the planted tile;
     _reduce_field_to_tiles matches _tile_rects (mean per tile, NaN when < 7px).
  5. probe_metrics JSON contract: all required top-level + per-selector keys, all four
     selectors present, available_selectors reflects availability, full 64-tile arrays,
     NaN -> null, worst-tile identity correct, json.dumps round-trips.
  6. The derived Blender script is a superset of the shared one and carries the two
     AOV-pass sentinels.
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

import exp_multi_selector_probe as probe  # noqa: E402
import exp_cross_denoiser_probe as xdp  # noqa: E402
import exp_render_stack as ers  # noqa: E402

GRID = ers.GRADING_TILE_GRID
NAN = float("nan")


def flat_matrix(values, grid=GRID):
    """Row-major 64-list -> [grid][grid] matrix."""
    assert len(values) == grid * grid
    return [list(values[r * grid:(r + 1) * grid]) for r in range(grid)]


def tile_index(gy, gx, grid=GRID):
    return gy * grid + gx


# --------------------------------------------------------------------------- #
# 1 + 2 + 3 — the per-selector metric block (recall@k, Spearman, availability).  #
# --------------------------------------------------------------------------- #
class TestSelectorEntry(unittest.TestCase):
    def _entry(self, sel_vals, E_vals, available=True, reason=None,
               name="S1_sample_count"):
        sel_mat = flat_matrix(sel_vals) if sel_vals is not None else None
        return probe._selector_entry(
            name, sel_mat, available, reason, list(E_vals), grid=GRID)

    def test_sample_count_ranks_true_worst_first(self):
        # THE headline case: the adaptive sample-count selector puts its highest
        # count on the SAME tile that carries the highest true error -> recall@1 == 1.0
        # and a positive Spearman (a hard-to-converge tile that is also the worst tile).
        worst = tile_index(6, 2)
        sc = [0.001 * (i + 1) for i in range(64)]   # smoothly increasing counts
        E = [0.001 * (i + 1) for i in range(64)]
        sc[worst] = 9.0     # by far the most samples spent here
        E[worst] = 0.09     # ...and by far the worst delivered error
        entry = self._entry(sc, E)
        self.assertTrue(entry["available"])
        self.assertEqual(entry["recall_at_1"], 1.0)
        self.assertEqual(entry["top_tile"]["tile"], [6, 2])
        self.assertGreater(entry["spearman_vs_E_oidn"], 0.9)
        self.assertEqual(entry["recall_k_eff"], {"1": 1, "4": 4, "12": 12})

    def test_monotone_agreement_scores_perfectly(self):
        sel = [0.001 * (i + 1) for i in range(64)]
        E = [0.002 * (i + 1) for i in range(64)]
        entry = self._entry(sel, E)
        self.assertEqual(entry["recall_at_1"], 1.0)
        self.assertEqual(entry["recall_at_4"], 1.0)
        self.assertEqual(entry["recall_at_12"], 1.0)
        self.assertAlmostEqual(entry["spearman_vs_E_oidn"], 1.0)

    def test_run4_failure_mode_scores_zero(self):
        # selector's top-4 tiles are {0..3}; the true-worst are {60..63} -> recall@4 == 0.
        sel = [0.001 * (i + 1) for i in range(64)]
        E = [0.001 * (i + 1) for i in range(64)]
        for i in range(4):
            sel[i] = 0.9 - 0.01 * i
            E[60 + i] = 0.9 - 0.01 * i
        entry = self._entry(sel, E)
        self.assertEqual(entry["recall_at_4"], 0.0)

    def test_partial_overlap(self):
        sel = [0.0] * 64
        E = [0.0] * 64
        for rank, i in enumerate((5, 6, 7, 8)):     # selector top-4
            sel[i] = 1.0 - 0.01 * rank
        for rank, i in enumerate((5, 6, 40, 41)):   # truth top-4 (2 shared)
            E[i] = 1.0 - 0.01 * rank
        entry = self._entry(sel, E)
        self.assertEqual(entry["recall_k_eff"]["4"], 4)
        self.assertAlmostEqual(entry["recall_at_4"], 0.5)

    def test_k_larger_than_finite_clamps(self):
        sel = [0.3, 0.1] + [NAN] * 62
        E = [0.2, 0.4] + [0.5 + 0.001 * i for i in range(62)]
        entry = self._entry(sel, E)
        # only indices {0,1} finite in the SELECTOR -> k_eff clamps to 2 for recall@4/@12
        self.assertEqual(entry["recall_k_eff"]["12"], 2)
        self.assertEqual(entry["recall_k_eff"]["4"], 2)

    def test_unavailable_selector_is_all_null_with_reason(self):
        entry = self._entry(None, [0.01] * 64, available=False,
                            reason="debug sample-count pass not written")
        self.assertFalse(entry["available"])
        self.assertIsNone(entry["recall_at_1"])
        self.assertIsNone(entry["recall_at_4"])
        self.assertIsNone(entry["recall_at_12"])
        self.assertIsNone(entry["spearman_vs_E_oidn"])
        self.assertIsNone(entry["tiles"])
        self.assertIsNone(entry["top_tile"])
        self.assertEqual(entry["top12"], [])
        self.assertEqual(entry["recall_k_eff"], {"1": 0, "4": 0, "12": 0})
        self.assertEqual(entry["unavailable_reason"], "debug sample-count pass not written")

    def test_unavailable_when_matrix_is_none_even_if_flagged_available(self):
        # Belt-and-suspenders: a None matrix is treated as unavailable regardless.
        entry = self._entry(None, [0.01] * 64, available=True, reason=None)
        self.assertFalse(entry["available"])
        self.assertIsNone(entry["tiles"])

    def test_nan_tile_becomes_null_and_shrinks_valid_count(self):
        sel = [0.001 * (i + 1) for i in range(64)]
        sel[5] = NAN
        entry = self._entry(sel, [0.002 * (i + 1) for i in range(64)])
        self.assertIsNone(entry["tiles"][5])
        self.assertEqual(entry["n_valid_tiles"], 63)

    def test_length_mismatch_raises(self):
        with self.assertRaises(ValueError):
            probe._selector_entry("S1_sample_count", [[0.1]], True, None,
                                  [0.0] * 64, grid=GRID)


# --------------------------------------------------------------------------- #
# 4 — the per-pixel selector FIELDS + the shared tile reduction.                #
# --------------------------------------------------------------------------- #
class TestReduceFieldToTiles(unittest.TestCase):
    def test_constant_field_uniform_means(self):
        field = np.full((96, 128), 0.7, dtype=np.float32)
        m = probe._reduce_field_to_tiles(field)
        self.assertEqual(m.shape, (GRID, GRID))
        finite = m[np.isfinite(m)]
        self.assertEqual(finite.size, GRID * GRID)
        self.assertTrue(np.allclose(finite, 0.7, atol=1e-6))

    def test_tiny_tiles_are_nan(self):
        # 32x32 on an 8x8 grid -> 4x4 tiles (< 7px) -> every tile NaN (mirrors the
        # SSIM grading skip so the selector universe matches E_oidn).
        field = np.ones((32, 32), dtype=np.float32)
        m = probe._reduce_field_to_tiles(field)
        self.assertTrue(np.all(np.isnan(m)))

    def test_planted_tile_is_argmax(self):
        field = np.zeros((96, 128), dtype=np.float32)
        rects = ers._tile_rects(96, 128, GRID)
        gy, gx, y0, y1, x0, x1 = rects[tile_index(3, 5)]
        field[y0:y1, x0:x1] = 5.0
        m = probe._reduce_field_to_tiles(field)
        self.assertEqual(int(np.nanargmax(m)), tile_index(3, 5))


class TestSelectorFields(unittest.TestCase):
    def _base(self, h=96, w=128, seed=7):
        rng = np.random.default_rng(seed)
        return np.clip(0.5 + 0.05 * rng.standard_normal((h, w, 3)), 0.01, None
                       ).astype(np.float32)

    def test_residual_field_localizes_denoiser_work(self):
        # noisy and delivered agree everywhere EXCEPT one tile, where the denoiser
        # "moved" the pixels a lot -> that tile is the residual argmax.
        oidn = self._base()
        noisy = oidn.copy()
        rects = ers._tile_rects(96, 128, GRID)
        gy, gx, y0, y1, x0, x1 = rects[tile_index(2, 6)]
        noisy[y0:y1, x0:x1] += 0.4
        m = probe._reduce_field_to_tiles(probe.residual_field(noisy, oidn))
        self.assertEqual(int(np.nanargmax(m)), tile_index(2, 6))

    def test_residual_shape_mismatch_raises(self):
        with self.assertRaises(RuntimeError):
            probe.residual_field(np.zeros((96, 128, 3), np.float32),
                                 np.zeros((96, 64, 3), np.float32))

    def test_gradient_field_localizes_high_frequency(self):
        # a flat frame with ONE noisy tile -> that tile has the most gradient energy.
        img = np.full((96, 128, 3), 0.5, dtype=np.float32)
        rng = np.random.default_rng(3)
        rects = ers._tile_rects(96, 128, GRID)
        gy, gx, y0, y1, x0, x1 = rects[tile_index(4, 1)]
        img[y0:y1, x0:x1] += 0.3 * rng.standard_normal((y1 - y0, x1 - x0, 3)).astype(np.float32)
        m = probe._reduce_field_to_tiles(probe.gradient_field(img))
        self.assertEqual(int(np.nanargmax(m)), tile_index(4, 1))

    def test_normal_edge_field_localizes_geometry(self):
        # flat normals (facing +Z) everywhere except an INNER block of one tile that
        # faces +X -> the geometric edge (its border) lives strictly inside that tile.
        normal = np.zeros((96, 128, 3), dtype=np.float32)
        normal[..., 2] = 1.0
        rects = ers._tile_rects(96, 128, GRID)
        gy, gx, y0, y1, x0, x1 = rects[tile_index(5, 3)]
        normal[y0 + 3:y1 - 3, x0 + 3:x1 - 3, :] = np.array([1.0, 0.0, 0.0], np.float32)
        m = probe._reduce_field_to_tiles(probe.normal_edge_field(normal))
        self.assertEqual(int(np.nanargmax(m)), tile_index(5, 3))

    def test_sample_count_field_end_to_end_recall(self):
        # A per-pixel sample-count map peaking in the true-worst tile, reduced and fed
        # as S1, must recover that tile: recall@1 == 1.0. (The pixel->tile->recall path.)
        sc = np.full((96, 128), 16.0, dtype=np.float32)
        rects = ers._tile_rects(96, 128, GRID)
        worst = tile_index(1, 7)
        gy, gx, y0, y1, x0, x1 = rects[worst]
        sc[y0:y1, x0:x1] = 480.0
        s1_mat = probe._reduce_field_to_tiles(sc)
        E = [0.001] * 64
        E[worst] = 0.09
        entry = probe._selector_entry("S1_sample_count", s1_mat, True, None,
                                      list(E), grid=GRID)
        self.assertEqual(entry["recall_at_1"], 1.0)


# --------------------------------------------------------------------------- #
# 5 — the full probe_metrics JSON contract.                                     #
# --------------------------------------------------------------------------- #
class TestProbeMetricsContract(unittest.TestCase):
    TOP_KEYS = [
        "probe", "label", "hypothesis", "grid", "n_tiles", "n_valid_E_tiles",
        "E_oidn_tiles", "worst_tile_by_E_oidn", "top12_by_E_oidn",
        "selectors", "selector_order", "available_selectors",
        "wall_anchor_oidn_s", "wall_noisy_s", "wall_ref_s",
        "device", "modeled", "note",
    ]
    SEL_KEYS = [
        "selector", "available", "unavailable_reason",
        "recall_at_1", "recall_at_4", "recall_at_12", "recall_k_eff",
        "spearman_vs_E_oidn", "n_valid_tiles", "tiles", "top_tile", "top12",
    ]

    def _metrics(self, s1_available=True, e_vals=None):
        E = e_vals if e_vals is not None else [0.001 * (i + 1) for i in range(64)]
        # S1 agrees with E; S2/S3/S4 are arbitrary but present.
        agree = flat_matrix(E)
        s2 = flat_matrix([0.5 - 0.001 * i for i in range(64)])
        s3 = flat_matrix([0.002 * (i % 8) for i in range(64)])
        s4 = flat_matrix([0.7] * 64)  # constant -> Spearman undefined (None)
        selectors = [
            ("S1_sample_count", agree if s1_available else None, s1_available,
             None if s1_available else "sample-count pass not written"),
            ("S2_denoiser_residual", s2, True, None),
            ("S3_content_gradient", s3, True, None),
            ("S4_aov_edge", s4, True, None),
        ]
        return probe.probe_metrics(
            flat_matrix(E), selectors, grid=GRID,
            params_echo={"scene": "synthetic", "draft_spp": 512},
            walls={"anchor_oidn": 203.1, "noisy": 190.4, "ref": 1136.7},
            device="GPU/OPTIX",
            quality_context={"ssim_global_oidn_vs_ref": 0.9854},
            note="synthetic contract test",
        )

    def test_top_level_keys_and_shapes(self):
        m = self._metrics()
        for key in self.TOP_KEYS:
            self.assertIn(key, m, key)
        self.assertEqual(m["grid"], GRID)
        self.assertEqual(m["n_tiles"], 64)
        self.assertEqual(len(m["E_oidn_tiles"]), 64)
        self.assertEqual(len(m["top12_by_E_oidn"]), 12)
        self.assertEqual(m["label"], "MEASURED")
        self.assertIs(m["modeled"], False)
        self.assertEqual(m["selector_order"],
                         list(probe.SELECTOR_ORDER))
        # params echo + quality context merged in
        self.assertEqual(m["scene"], "synthetic")
        self.assertEqual(m["draft_spp"], 512)
        self.assertEqual(m["ssim_global_oidn_vs_ref"], 0.9854)

    def test_all_four_selectors_present_with_required_keys(self):
        m = self._metrics()
        self.assertEqual(set(m["selectors"].keys()), set(probe.SELECTOR_ORDER))
        for name, block in m["selectors"].items():
            for key in self.SEL_KEYS:
                self.assertIn(key, block, f"{name}.{key}")
            self.assertEqual(block["selector"], name)

    def test_available_selectors_reflects_availability(self):
        m = self._metrics(s1_available=True)
        self.assertIn("S1_sample_count", m["available_selectors"])
        m2 = self._metrics(s1_available=False)
        self.assertNotIn("S1_sample_count", m2["available_selectors"])
        self.assertEqual(
            set(m2["available_selectors"]),
            {"S2_denoiser_residual", "S3_content_gradient", "S4_aov_edge"})
        self.assertFalse(m2["selectors"]["S1_sample_count"]["available"])
        self.assertIsNone(m2["selectors"]["S1_sample_count"]["tiles"])

    def test_s1_agreement_recovers_worst_tile(self):
        # E strictly increasing -> worst tile is index 63; S1 mirrors E -> recall 1.
        m = self._metrics()
        self.assertEqual(m["worst_tile_by_E_oidn"]["tile"], [7, 7])
        s1 = m["selectors"]["S1_sample_count"]
        self.assertEqual(s1["recall_at_1"], 1.0)
        self.assertEqual(s1["recall_at_12"], 1.0)
        self.assertAlmostEqual(s1["spearman_vs_E_oidn"], 1.0)

    def test_constant_selector_spearman_is_none(self):
        # S4 is constant here -> Spearman undefined -> None (never fabricated).
        m = self._metrics()
        self.assertIsNone(m["selectors"]["S4_aov_edge"]["spearman_vs_E_oidn"])

    def test_nan_e_tile_becomes_null_and_shrinks_valid(self):
        E = [0.001 * (i + 1) for i in range(64)]
        E[9] = NAN
        m = self._metrics(e_vals=E)
        self.assertIsNone(m["E_oidn_tiles"][9])
        self.assertEqual(m["n_valid_E_tiles"], 63)

    def test_json_round_trip_no_nan_token(self):
        E = [0.001 * (i + 1) for i in range(64)]
        E[0] = NAN
        m = self._metrics(e_vals=E)
        s = json.dumps(m)
        self.assertNotIn("NaN", s)
        self.assertEqual(json.loads(s)["selectors"]["S2_denoiser_residual"]["selector"],
                         "S2_denoiser_residual")

    def test_wrong_e_tile_count_raises(self):
        with self.assertRaises(ValueError):
            probe.probe_metrics(
                [[0.1]], [("S1_sample_count", None, False, "x")],
                grid=GRID, params_echo={},
                walls={"anchor_oidn": 1, "noisy": 1, "ref": 1}, device="GPU", note="")


# --------------------------------------------------------------------------- #
# 6 — the derived Blender script stays a superset of the shared one.            #
# --------------------------------------------------------------------------- #
class TestDerivedBlenderScript(unittest.TestCase):
    def test_superset_and_sentinels(self):
        plus = probe.BLENDER_SCENE_SCRIPT_PLUS
        base = ers.BLENDER_SCENE_SCRIPT
        # every non-empty line of the shared script survives in the derived one
        self.assertIn("vl.use_pass_combined = True", plus)
        self.assertIn("bpy.ops.render.render(write_still=True)", plus)
        # the two AOV passes + their availability sentinels are injected
        self.assertIn("use_pass_normal", plus)
        self.assertIn("use_pass_debug_sample_count", plus)
        self.assertIn("CX_NORMAL_PASS=1", plus)
        self.assertIn("CX_SAMPLECOUNT_PASS", plus)
        self.assertIn("CX_WANT_NORMAL", plus)
        self.assertIn("CX_WANT_SAMPLECOUNT", plus)
        # derivation added content (it is a strict superset in length)
        self.assertGreater(len(plus), len(base))

    def test_compiles_as_python(self):
        # the embedded Blender script must be syntactically valid python
        compile(probe.BLENDER_SCENE_SCRIPT_PLUS, "<blender_scene_plus>", "exec")


if __name__ == "__main__":
    unittest.main(verbosity=2)
