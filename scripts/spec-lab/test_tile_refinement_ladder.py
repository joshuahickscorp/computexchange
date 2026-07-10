import os
import sys
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import run_tile_refinement_ladder as ladder  # noqa: E402


class TileRefinementLadderTests(unittest.TestCase):
    def test_tile_bounds_cover_image_edges(self):
        self.assertEqual(ladder.tile_bounds(1024, 512, 8, 0, 0), (0, 0, 128, 64))
        self.assertEqual(ladder.tile_bounds(1024, 512, 8, 7, 7), (896, 448, 1024, 512))
        self.assertEqual(ladder.tile_bounds(10, 7, 3, 2, 2), (6, 4, 10, 7))

    def test_cx_crop_for_tile_converts_top_left_y_to_cycles_full_y(self):
        self.assertEqual(
            ladder.cx_crop_for_tile(1024, 512, 0, 0, 128, 64),
            "0,448,128,64,1024,512",
        )

    def test_cx_crop_for_bottom_tile_uses_zero_full_y(self):
        self.assertEqual(
            ladder.cx_crop_for_tile(1024, 512, 896, 448, 1024, 512),
            "896,0,128,64,1024,512",
        )

    def test_classify_requires_worst_tile(self):
        self.assertEqual(ladder.classify(0.999, 0.70), "fail")
        self.assertEqual(ladder.classify(0.95, 0.90), "preview")
        self.assertEqual(ladder.classify(0.99, 0.96), "delivery")

    def test_dry_command_contains_batch_crop_manifest(self):
        cmd = ladder.tile_refinement_cmd(
            "/root/cx-cycles",
            "scene_cube_volume.xml",
            4096,
            16,
            32,
            "CUDA",
            True,
            8,
            0.95,
            4,
        )
        self.assertIn("--cx-batch-manifest", cmd)
        self.assertIn("cx_batch_crop_manifest", cmd)
        self.assertIn("render_crops_batch", cmd)
        self.assertIn("crop_spec.replace(',', ' ')", cmd)
        self.assertIn("CX_TILE_REFINE_ROW", cmd)


if __name__ == "__main__":
    unittest.main()
