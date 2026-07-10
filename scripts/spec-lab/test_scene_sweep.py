#!/usr/bin/env python3
"""Unit tests for run_scene_sweep.py (config/manifest/quote build) and
calibrate_repair_budget.py (selection semantics + projection math), plus a
real-ledger integration check against the banked GROW capstone receipt.

Run:  python3 scripts/spec-lab/test_scene_sweep.py
"""

from __future__ import annotations

import argparse
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "pod"))

import calibrate_repair_budget as calib  # noqa: E402
import run_scene_sweep as sweep  # noqa: E402


def sweep_args(**overrides) -> argparse.Namespace:
    """The driver's argparse defaults, as a Namespace (kept in one place so a
    default drift between the tests and main() fails loudly here)."""
    defaults = dict(
        sweep_id="test-sweep",
        scenes=",".join(e["key"] for e in sweep.SCENE_MATRIX),
        resolution="1920x1080",
        frames=4,
        draft_spp=192,
        ref_spp=1536,
        repair_top_k=32,
        repair_max_per_frame=8,
        repair_min_divergence=0.0,
        events=1024,
        prefix_rows=12,
        max_minutes_per_scene=60,
        min_balance=6.0,
        gpu_plan=sweep.production.DEFAULT_GPU_PLAN,
        fail_fast=False,
        dry_run=True,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestSceneMatrix(unittest.TestCase):
    def test_matrix_has_control_plus_two_diverse_scenes(self):
        keys = [e["key"] for e in sweep.SCENE_MATRIX]
        self.assertIn("classroom", keys)
        self.assertGreaterEqual(len(keys), 3)
        families = {e["family"] for e in sweep.SCENE_MATRIX}
        self.assertGreaterEqual(len(families), 3, "scenes must be diverse, not clones")

    def test_every_scene_resolves_through_the_runner(self):
        """Native keys must exist in exp_render_stack.SCENE_SOURCES; URL entries
        must be direct http(s) .zip/.blend links (the two forms resolve_scene
        accepts). junkshop is banned: the runner silently falls back to classroom."""
        import exp_render_stack as runner
        for entry in sweep.SCENE_MATRIX:
            arg = entry["scene_arg"]
            self.assertNotEqual(arg, "junkshop")
            if arg.startswith("http://") or arg.startswith("https://"):
                self.assertTrue(
                    arg.split("?")[0].endswith((".zip", ".blend")),
                    f"{entry['key']}: URL must be a direct .zip/.blend: {arg}",
                )
            else:
                self.assertIn(arg, runner.SCENE_SOURCES,
                              f"{entry['key']}: not a native runner scene key")
            self.assertTrue(entry.get("verified"), f"{entry['key']}: evidence required")

    def test_excluded_scenes_documented(self):
        self.assertIn("junkshop", sweep.EXCLUDED_SCENES)
        for reason in sweep.EXCLUDED_SCENES.values():
            self.assertGreater(len(reason), 10)


class TestBuildSweepConfig(unittest.TestCase):
    def setUp(self):
        self.entry = sweep.SCENE_MATRIX[0]
        self.config = sweep.build_sweep_config(self.entry, sweep_args())

    def test_proven_grow_recipe_fields(self):
        """Field-for-field the strict-delivery GROW recipe at 1080p scale."""
        c = self.config
        self.assertEqual(c["keyframe_every"], 1)
        self.assertEqual(c["draft_spp"], 192)
        self.assertEqual(c["ref_spp"], 1536)
        self.assertEqual(c["adaptive_threshold"], 0.02)
        self.assertEqual(c["denoiser"], "oidn")
        self.assertTrue(c["denoise_guides"])
        self.assertTrue(c["light_tree"])
        self.assertEqual(c["bounces"], 12)
        self.assertTrue(c["repair_enabled"])
        self.assertEqual(c["repair_selector"], "aov_edge")
        self.assertEqual(c["repair_denoiser"], "none")
        self.assertEqual(c["repair_spp"], c["ref_spp"], "match-reference raw repair")
        self.assertEqual(c["repair_top_k"], 32)
        self.assertEqual(c["repair_max_per_frame"], 8)
        self.assertEqual(c["repair_min_divergence"], 0.0, "rank-only = proven default")

    def test_money_and_device_safety_fields(self):
        self.assertEqual(self.config["device"], "GPU")
        self.assertTrue(self.config["require_gpu"], "fail-loud CPU-fallback guard")
        self.assertEqual(self.config["gpu_probe_timeout_s"], 1500, "sm_90 JIT headroom")

    def test_no_per_scene_tuning(self):
        """Objective 1: the recipe transfers WITHOUT per-scene tuning — every scene
        gets a config differing ONLY in the scene argument."""
        args = sweep_args()
        configs = [sweep.build_sweep_config(e, args) for e in sweep.SCENE_MATRIX]
        stripped = [{k: v for k, v in c.items() if k != "scene"} for c in configs]
        self.assertTrue(all(s == stripped[0] for s in stripped))
        self.assertEqual(len({c["scene"] for c in configs}), len(configs))

    def test_min_divergence_passthrough(self):
        c = sweep.build_sweep_config(self.entry, sweep_args(repair_min_divergence=0.0928))
        self.assertEqual(c["repair_min_divergence"], 0.0928)


class TestQuoteAndManifest(unittest.TestCase):
    def test_quote_positive_and_labeled(self):
        config = sweep.build_sweep_config(sweep.SCENE_MATRIX[0], sweep_args())
        q = sweep.estimate_scene_cost(config, first_scene=True)
        self.assertGreater(q["total_s"], 0)
        self.assertGreater(q["usd_at_a100_secure"], 0)
        self.assertIn("MODELED", q["label"])
        self.assertIn("MEASURED", q["label"])

    def test_quote_uses_measured_1080p_anchors(self):
        """At the measured basis point the ref/anchor terms must reproduce the
        A100 ledger numbers exactly (105.65*4 / 27.14*4) — no silent rescale."""
        config = sweep.build_sweep_config(sweep.SCENE_MATRIX[0], sweep_args())
        q = sweep.estimate_scene_cost(config)
        self.assertAlmostEqual(q["ref_render_s"], 105.65 * 4, delta=0.5)
        self.assertAlmostEqual(q["anchor_render_s"], 27.14 * 4, delta=0.5)

    def test_quote_monotonic_in_resolution_and_setup(self):
        args_1080 = sweep_args()
        args_4k = sweep_args(resolution="3840x2160", draft_spp=512, ref_spp=4096)
        entry = sweep.SCENE_MATRIX[0]
        q1080 = sweep.estimate_scene_cost(sweep.build_sweep_config(entry, args_1080))
        q4k = sweep.estimate_scene_cost(sweep.build_sweep_config(entry, args_4k))
        self.assertGreater(q4k["total_s"], q1080["total_s"])
        first = sweep.estimate_scene_cost(sweep.build_sweep_config(entry, args_1080), True)
        self.assertGreater(first["total_s"], q1080["total_s"], "setup charged once")

    def test_repair_area_fraction_bounds(self):
        f = sweep.tile_repair_area_fraction("1920x1080", 8)
        self.assertGreater(f, 0.0)
        self.assertLess(f, 0.5, "8 bordered tiles of an 8x8 grid stay well under half")
        self.assertEqual(sweep.tile_repair_area_fraction("64x64", 64), 1.0, "clamped")

    def test_manifest_shape_and_totals(self):
        m = sweep.build_sweep_manifest(sweep_args())
        self.assertEqual(len(m["scenes"]), len(sweep.SCENE_MATRIX))
        self.assertEqual(m["per_scene_timeout_s"], 3600)
        self.assertEqual(m["watchdog_ttl_s"], 1800 + 3 * 3600 + 1800)
        total = round(sum(s["quote"]["usd_at_a100_secure"] for s in m["scenes"]), 2)
        self.assertAlmostEqual(m["quote_total_usd_at_a100_secure"], total, places=2)
        self.assertIn("excluded_scenes", m)

    def test_manifest_rejects_unknown_scene(self):
        with self.assertRaises(ValueError):
            sweep.build_sweep_manifest(sweep_args(scenes="classroom,junkshop"))

    def test_manifest_scene_subset(self):
        m = sweep.build_sweep_manifest(sweep_args(scenes="bmw27"))
        self.assertEqual([s["scene"]["key"] for s in m["scenes"]], ["bmw27"])

    def test_gpu_plan_blackwell_guard_still_bites(self):
        """The imported parse_gpu_plan must keep rejecting Blackwell SKUs."""
        with self.assertRaises(ValueError):
            sweep.production.parse_gpu_plan("NVIDIA B300:SECURE")


def synth_tiles():
    """4 frames x 3 scored tiles with known labels. Needed (<0.95): the score-0.30
    tile of every frame and the score-0.18 tile of frame 3 (5 needed of 12)."""
    tiles = []
    for f in range(4):
        tiles.append({"frame": f, "tile": (1, 7), "score": 0.30, "ssim_pre": 0.91,
                      "ssim_after": 1.0})
        tiles.append({"frame": f, "tile": (2, 7), "score": 0.20, "ssim_pre": 0.97,
                      "ssim_after": 1.0})
        pre = 0.94 if f == 3 else 0.96
        tiles.append({"frame": f, "tile": (0, 7), "score": 0.18, "ssim_pre": pre,
                      "ssim_after": 1.0})
    return tiles


def synth_costs():
    return {
        "T_ref_s": 1000.0,
        "T_stack_s": 400.0,
        "repair_cost_s": 240.0,       # 12 tiles x (15 render + 5 composite)
        "selection_cost_s": 20.0,
        "per_tile_render_s": 15.0,
        "per_tile_composite_s": 5.0,
        "per_frame_composite_s": 15.0,  # 60 composite total / 4 frames
        "n_repaired": 12.0,
    }


class TestCalibrationMath(unittest.TestCase):
    def test_select_by_floor_is_strict_and_capped(self):
        tiles = synth_tiles()
        self.assertEqual(len(calib.select_by_floor(tiles, 0.30)), 0, "strict >")
        self.assertEqual(len(calib.select_by_floor(tiles, 0.299999)), 4)
        self.assertEqual(len(calib.select_by_floor(tiles, 0.0)), 12)
        capped = calib.select_by_floor(tiles, 0.0, top_k=8, max_per_frame=2)
        self.assertEqual(len(capped), 8)
        for f in range(4):
            self.assertLessEqual(sum(1 for t in capped if t["frame"] == f), 2)

    def test_select_matches_runner_semantics(self):
        """Cross-check against the REAL pod runner's select_repair_tiles on the
        same synthetic score field — the calibration must never diverge from the
        code that will act on its threshold."""
        import numpy as np
        import exp_render_stack as runner
        tiles = synth_tiles()
        scores_per_frame = []
        for f in range(4):
            grid = np.zeros((8, 8), dtype=np.float64)
            for t in tiles:
                if t["frame"] == f:
                    grid[t["tile"][0], t["tile"][1]] = t["score"]
            scores_per_frame.append(grid)
        for floor in (0.0, 0.18, 0.19, 0.25, 0.30):
            for top_k, mpf in ((32, 8), (8, 2), (4, 1)):
                runner_pick = {
                    (f, (gy, gx))
                    for f, gy, gx, _ in runner.select_repair_tiles(
                        scores_per_frame, top_k, mpf, min_divergence=floor)
                }
                ours = {
                    (t["frame"], t["tile"])
                    for t in calib.select_by_floor(tiles, floor, top_k=top_k,
                                                   max_per_frame=mpf)
                }
                self.assertEqual(runner_pick, ours,
                                 f"divergence at floor={floor} top_k={top_k} mpf={mpf}")

    def test_policy_stats_recall_and_over_repair(self):
        tiles = synth_tiles()
        stats = calib.policy_stats(calib.select_by_floor(tiles, 0.299999), tiles, 0.95)
        self.assertEqual(stats["n_needed"], 5)
        self.assertEqual(stats["n_caught"], 4)
        self.assertAlmostEqual(stats["recall"], 0.8)
        self.assertEqual(stats["n_over_repair"], 0)
        stats_all = calib.policy_stats(calib.select_by_floor(tiles, 0.0), tiles, 0.95)
        self.assertEqual(stats_all["recall"], 1.0)
        self.assertEqual(stats_all["n_over_repair"], 7)

    def test_projection_reproduces_measured_run_at_full_count(self):
        """n = all repaired tiles => the linear model must give back the MEASURED
        T_stack exactly (no free speedup from the modeling itself)."""
        costs = synth_costs()
        proj = calib.project_speedup(costs, 12, 4)
        self.assertAlmostEqual(proj["T_stack_linear_s"], costs["T_stack_s"], places=6)
        self.assertAlmostEqual(proj["speedup_linear_x"],
                               costs["T_ref_s"] / costs["T_stack_s"], places=4)

    def test_projection_hand_computed(self):
        costs = synth_costs()
        proj = calib.project_speedup(costs, 4, 4)
        # base 160 + 4*(15+5) = 240 linear; base 160 + 4*15 + 4*15 = 280 conservative
        self.assertAlmostEqual(proj["T_stack_linear_s"], 240.0, places=6)
        self.assertAlmostEqual(proj["T_stack_conservative_s"], 280.0, places=6)
        self.assertAlmostEqual(proj["speedup_linear_x"], 1000.0 / 240.0, places=3)
        self.assertLessEqual(proj["speedup_conservative_x"], proj["speedup_linear_x"])

    def test_recommend_floor_sits_in_the_recall1_gap(self):
        tiles = synth_tiles()
        rank_rows = calib.build_rank_table(tiles, synth_costs(), 0.95, 4)
        rec = calib.recommend(tiles, rank_rows, synth_costs(), 0.95)
        floor = rec["recommended_repair_min_divergence"]
        self.assertLess(floor, 0.18, "must keep the weakest needed tile (score 0.18)")
        self.assertEqual(rec["floor_policy"]["recall"], 1.0)
        self.assertEqual(rec["oracle_ceiling"]["n_selected"], 5)
        self.assertIn("UNVALIDATED", rec["caveat"])

    def test_coverage_complete_logic(self):
        self.assertTrue(calib.coverage_complete(
            {"per_frame_worst_tile_ssim": [0.9539, 0.9501, 0.9506, 0.9531]}, 0.95))
        self.assertFalse(calib.coverage_complete(
            {"per_frame_worst_tile_ssim": [0.9539, 0.9281, 0.9506, 0.9531]}, 0.95))
        self.assertFalse(calib.coverage_complete({"per_frame_worst_tile_ssim": []}, 0.95))


class TestRealLedgerIntegration(unittest.TestCase):
    """Against the ACTUAL banked capstone ledger (read-only)."""
    LEDGER = REPO / "docs/speed-lane-reports/spec-lab/integrated_spec_render_token_ledger.jsonl"

    def setUp(self):
        if not self.LEDGER.is_file():
            self.skipTest("banked capstone ledger not present")
        self.receipts = calib.load_repair_receipts([self.LEDGER], "aov_edge")

    def test_grow_capstone_found_and_complete(self):
        complete = [r for r in self.receipts if calib.coverage_complete(r["rm"], 0.95)]
        self.assertGreaterEqual(len(self.receipts), 3, "three aov_edge repair receipts banked")
        self.assertEqual(len(complete), 1, "exactly the GROW capstone has full coverage")
        rm = complete[0]["rm"]
        self.assertEqual(rm["repaired_tile_count"], 32)
        tiles = calib.extract_tiles(rm)
        needed = [t for t in tiles if t["ssim_pre"] < 0.95]
        self.assertEqual(len(needed), 8, "8 sub-gate tiles in the GROW run")

    def test_recall1_threshold_beats_measured_speedup(self):
        complete = [r for r in self.receipts if calib.coverage_complete(r["rm"], 0.95)][0]
        rm = complete["rm"]
        tiles = calib.extract_tiles(rm)
        costs = calib.measured_costs(rm)
        rank_rows = calib.build_rank_table(tiles, costs, 0.95, int(rm["frames"]))
        rec = calib.recommend(tiles, rank_rows, costs, 0.95)
        measured = costs["T_ref_s"] / costs["T_stack_s"]
        self.assertEqual(rec["floor_policy"]["recall"], 1.0)
        self.assertGreater(rec["floor_policy"]["speedup_conservative_x"], measured,
                           "the calibrated budget must project ABOVE the measured 2.45x")
        self.assertGreater(rec["oracle_ceiling"]["speedup_conservative_x"],
                           rec["floor_policy"]["speedup_conservative_x"] - 1e-9)


class TestDryRunNeverProvisions(unittest.TestCase):
    def test_manifest_build_is_pure(self):
        """build_sweep_manifest must not touch runpod state (no tracking file, no
        API): it only parses the plan and does arithmetic."""
        before = json.dumps(sweep.runpod._load_tracked())
        sweep.build_sweep_manifest(sweep_args())
        self.assertEqual(json.dumps(sweep.runpod._load_tracked()), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
