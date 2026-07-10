#!/usr/bin/env python3
"""Local correctness checks for the exp_render_stack.py REPAIR loop (PASS 3.5).

Everything here is LOCAL + SYNTHETIC — no GPU, no Blender, no cloud, no money. It
proves, on synthetic numpy tile arrays and a fully stubbed runner:

  1. _tile_rects is behavior-identical to the grading loop it was factored from
     (4K / 1080p / remainder / tiny cases) — so a repaired tile maps 1:1 onto a
     graded tile.
  2. The reference-free selector ranks planted high-variance tiles first on
     constructed noise fields, and select_repair_tiles enforces the global budget,
     the per-frame cap, the divergence floor and deterministic tie-breaks.
  3. REFERENCE-FREEDOM IS STRUCTURAL: the PASS 3.5 source block never touches
     true_colors, and no repair-path helper even accepts a reference argument.
  4. Feathered compositing is seam-safe: alpha == 1 across the ENTIRE graded tile,
     0 outside the bordered region, continuous everywhere (max step 1/feather);
     output finite; neighbor tiles degrade by no more than a noise epsilon.
  5. The numpy-rect -> Blender-border Y-flip round-trips, and the embedded
     BLENDER_SCENE_SCRIPT carries the exact same formulas (lockstep guard).
  6. Honest accounting: T_stack equals the exact sum of every charged stage
     (anchors + calibration + selection drafts + divergence scoring + repair
     renders + compositing); the selection draft is charged even at zero repairs.
  7. repair_enabled=False (the default) leaves the legacy metrics byte-identical
     on a stubbed run: params {} and {"repair_enabled": false} emit the SAME JSON
     bytes, with EXACTLY the frozen legacy key set and no repair key.
  8. Both embedded Blender scripts still compile.
  9. The adapter maps a repair-carrying metrics dict onto real per-tile
     accepted/repaired fractions without double-charging T_stack.
"""

import importlib.util
import inspect
import json
import os
import sys
import tempfile
import types
import unittest
import zlib

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
POD = os.path.join(HERE, "pod")
STACK_PATH = os.path.join(POD, "exp_render_stack.py")
sys.path.insert(0, HERE)
sys.path.insert(0, POD)

import cx_render_spec_adapter as rsa  # noqa: E402
import exp_render_stack as ers  # noqa: E402

GRID = ers.GRADING_TILE_GRID

# The EXACT metrics key set the legacy (pre-repair) runner emits. A default-off run
# must emit EXACTLY these keys — nothing repair-shaped may leak into legacy receipts.
LEGACY_METRICS_KEYS = [
    "net_speedup", "quality", "worst_tile_ssim", "p5_tile_ssim", "frames",
    "keyframes", "keyframe_every", "T_ref_s", "T_stack_s", "ref_cache_hit",
    "reproject_accept_frac", "mean_disoccluded_frac", "draft_spp", "ref_spp",
    "adaptive_threshold", "adaptive_min_samples", "denoiser", "denoise_guides",
    "light_tree", "bounces", "resolution", "scene", "hole_fill",
    "disocclusion_thresh", "cam_motion", "fixed_overhead_s",
    "mean_keyframe_render_s", "mean_keyframe_pixel_trace_s", "device", "modeled",
    "note", "per_frame_ref_s", "per_keyframe_render_s", "keyframe_indices",
    "reprojected_frames", "per_frame_global_ssim", "per_frame_worst_tile_ssim",
    "requested_scene",
]


# --------------------------------------------------------------------------- #
# 1. _tile_rects parity with the legacy inline grading loop                     #
# --------------------------------------------------------------------------- #
def _legacy_tile_rects(h, w, grid=8):
    """The loop exactly as it was inlined in compute_ssim_global_and_tiles before
    the refactor — the parity oracle."""
    ty = max(1, h // grid)
    tx = max(1, w // grid)
    rects = []
    for gy in range(grid):
        y0 = gy * ty
        y1 = h if gy == grid - 1 else (gy + 1) * ty
        for gx in range(grid):
            x0 = gx * tx
            x1 = w if gx == grid - 1 else (gx + 1) * tx
            rects.append((gy, gx, y0, y1, x0, x1))
    return rects


class TileRectsTest(unittest.TestCase):
    def test_parity_with_legacy_grading_loop(self):
        for h, w in [(2160, 3840), (1080, 1920), (1082, 1922), (50, 100), (7, 7),
                     (16, 16), (135, 241)]:
            self.assertEqual(ers._tile_rects(h, w, 8), _legacy_tile_rects(h, w, 8),
                             msg=f"rect mismatch at {w}x{h}")

    def test_4k_tiles_are_exact_480x270(self):
        rects = ers._tile_rects(2160, 3840, 8)
        self.assertEqual(len(rects), 64)
        for _gy, _gx, y0, y1, x0, x1 in rects:
            self.assertEqual(y1 - y0, 270)
            self.assertEqual(x1 - x0, 480)


# --------------------------------------------------------------------------- #
# synthetic frame builders (shared)                                             #
# --------------------------------------------------------------------------- #
def _base_image(h, w):
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    return np.stack([
        0.2 + 0.6 * xx / max(w, 1),
        0.2 + 0.6 * yy / max(h, 1),
        0.4 + 0.2 * (xx + yy) / max(h + w, 1),
    ], axis=-1)


def _plant_noise(img, tiles, rs, std):
    h, w = img.shape[:2]
    rect_by_tile = {(gy, gx): (y0, y1, x0, x1)
                    for gy, gx, y0, y1, x0, x1 in ers._tile_rects(h, w, GRID)}
    for gy, gx in tiles:
        y0, y1, x0, x1 = rect_by_tile[(gy, gx)]
        img[y0:y1, x0:x1] += rs.normal(0, std, (y1 - y0, x1 - x0, 3)).astype(np.float32)


# --------------------------------------------------------------------------- #
# 2. selector: high-variance tiles rank first; policy caps enforced             #
# --------------------------------------------------------------------------- #
class SelectorTest(unittest.TestCase):
    def test_planted_high_variance_tiles_rank_first(self):
        h, w = 128, 128
        planted = [(1, 2), (5, 6), (3, 0)]
        rs = np.random.RandomState(7)
        base = _base_image(h, w)
        a = base + rs.normal(0, 0.01, base.shape).astype(np.float32)
        b = base + rs.normal(0, 0.01, base.shape).astype(np.float32)
        _plant_noise(a, planted, rs, 0.35)
        _plant_noise(b, planted, rs, 0.35)  # independent realization, same tiles hot
        scores = ers.tile_divergence_scores(a, b, grid=GRID)
        self.assertEqual(scores.shape, (GRID, GRID))
        flat_order = np.argsort(scores, axis=None)[::-1]
        top = {(int(i // GRID), int(i % GRID)) for i in flat_order[:len(planted)]}
        self.assertEqual(top, set(planted))
        planted_min = min(scores[gy, gx] for gy, gx in planted)
        unplanted_max = max(scores[gy, gx] for gy in range(GRID) for gx in range(GRID)
                            if (gy, gx) not in planted)
        self.assertGreater(planted_min, unplanted_max)

    def test_select_repair_tiles_budget_and_per_frame_cap(self):
        s0 = np.zeros((GRID, GRID))
        s1 = np.zeros((GRID, GRID))
        s0[0, 0], s0[0, 1], s0[0, 2] = 0.9, 0.8, 0.7
        s1[3, 3] = 0.6
        picked = ers.select_repair_tiles([s0, s1], budget=3, max_per_frame=2,
                                         min_divergence=0.0)
        self.assertEqual([(f, gy, gx) for f, gy, gx, _ in picked],
                         [(0, 0, 0), (0, 0, 1), (1, 3, 3)])  # frame-0 cap hits at 2

    def test_select_repair_tiles_floor_and_zero_never_selected(self):
        s = np.zeros((GRID, GRID))
        s[2, 2] = 0.05
        # floor above the only candidate -> nothing selected even with budget slack
        self.assertEqual(ers.select_repair_tiles([s], 10, 10, min_divergence=0.1), [])
        # zero-divergence tiles are NEVER selected (rank-only default)
        picked = ers.select_repair_tiles([s], 10, 10, min_divergence=0.0)
        self.assertEqual([(f, gy, gx) for f, gy, gx, _ in picked], [(0, 2, 2)])

    def test_select_repair_tiles_deterministic_tiebreak(self):
        s0 = np.zeros((GRID, GRID))
        s1 = np.zeros((GRID, GRID))
        s0[4, 4] = 0.5
        s0[1, 1] = 0.5
        s1[0, 0] = 0.5
        picked = ers.select_repair_tiles([s0, s1], budget=2, max_per_frame=2,
                                         min_divergence=0.0)
        # equal scores -> (frame, gy, gx) ascending
        self.assertEqual([(f, gy, gx) for f, gy, gx, _ in picked],
                         [(0, 1, 1), (0, 4, 4)])


# --------------------------------------------------------------------------- #
# 3. reference-freedom is structural                                            #
# --------------------------------------------------------------------------- #
class ReferenceFreedomTest(unittest.TestCase):
    def test_pass_3_5_source_never_touches_the_reference(self):
        with open(STACK_PATH) as f:
            src = f.read()
        begin = src.index("PASS 3.5 — REPAIR (reference-free) BEGIN")
        end = src.index("PASS 3.5 — REPAIR (reference-free) END")
        self.assertLess(begin, end)
        block = src[begin:end]
        self.assertNotIn("true_colors", block,
                         "the repair pass must NEVER read the reference frames")
        self.assertNotIn("per_frame_ref", block)

    def test_repair_helpers_accept_no_reference_argument(self):
        for fn in (ers.tile_divergence_scores, ers.select_repair_tiles,
                   ers.merge_and_margin_rects, ers.build_feather_alpha,
                   ers.feather_composite):
            params = set(inspect.signature(fn).parameters)
            self.assertFalse(
                params & {"true_rgb", "true_colors", "reference", "ref", "truth"},
                f"{fn.__name__} must not accept a reference argument: {params}")

    def test_selection_happens_before_grading_in_source(self):
        with open(STACK_PATH) as f:
            src = f.read()
        repair_end = src.index("PASS 3.5 — REPAIR (reference-free) END")
        grading = src.index("END-TO-END SSIM: our DELIVERED frame vs the TRUE frame")
        self.assertLess(repair_end, grading,
                        "repair (delivery) must complete before SSIM-vs-reference grading")


# --------------------------------------------------------------------------- #
# 4. feathered compositing is seam-safe                                          #
# --------------------------------------------------------------------------- #
class FeatherCompositeTest(unittest.TestCase):
    def setUp(self):
        self.h = self.w = 128
        self.rects = {(gy, gx): (y0, y1, x0, x1)
                      for gy, gx, y0, y1, x0, x1 in ers._tile_rects(self.h, self.w, GRID)}

    def test_alpha_one_on_whole_graded_tile_zero_outside_border(self):
        core = self.rects[(3, 3)]  # y[48,64) x[48,64)
        margin, feather = 16, 12
        alpha = ers.build_feather_alpha(core, margin, feather, self.h, self.w)
        y0, y1, x0, x1 = core
        # alpha == 1 across the ENTIRE graded tile...
        self.assertTrue(np.all(alpha[y0:y1, x0:x1] == 1.0))
        # ...and across the innermost (margin - feather) px of the margin
        self.assertTrue(np.all(alpha[y0 - 4:y1 + 4, x0 - 4:x1 + 4] == 1.0))
        # alpha == 0 at Chebyshev distance >= margin (outside the bordered region)
        self.assertEqual(float(alpha[y0 - margin, x0]), 0.0)
        self.assertEqual(float(alpha[y1 - 1 + margin, x1 - 1]), 0.0)
        self.assertEqual(float(alpha[0, 0]), 0.0)

    def test_alpha_is_continuous(self):
        core = self.rects[(2, 5)]
        margin, feather = 16, 12
        alpha = ers.build_feather_alpha(core, margin, feather, self.h, self.w)
        max_step = 1.0 / feather + 1e-6
        self.assertLessEqual(float(np.abs(np.diff(alpha, axis=0)).max()), max_step)
        self.assertLessEqual(float(np.abs(np.diff(alpha, axis=1)).max()), max_step)

    def test_composite_finite_and_borders_continuous(self):
        core = self.rects[(3, 3)]
        margin, feather = 16, 12
        alpha = ers.build_feather_alpha(core, margin, feather, self.h, self.w)
        delivered = np.full((self.h, self.w, 3), 0.3, np.float32)
        repair = np.full((self.h, self.w, 3), 0.7, np.float32)
        comp = ers.feather_composite(delivered, repair, alpha)
        self.assertTrue(np.isfinite(comp).all())
        # worst-case constant-vs-constant seam: adjacent-pixel jump bounded by the ramp
        max_jump = 0.4 / feather + 1e-5
        self.assertLessEqual(float(np.abs(np.diff(comp, axis=0)).max()), max_jump)
        self.assertLessEqual(float(np.abs(np.diff(comp, axis=1)).max()), max_jump)
        # the graded tile is PURE repair pixels; far corners untouched
        y0, y1, x0, x1 = core
        self.assertTrue(np.all(comp[y0:y1, x0:x1] == 0.7))
        self.assertTrue(np.all(comp[0, 0] == 0.3))

    def test_neighbor_tiles_degrade_no_more_than_noise_epsilon(self):
        from skimage.metrics import structural_similarity as ssim
        rs = np.random.RandomState(11)
        base = _base_image(self.h, self.w)
        delivered = base + rs.normal(0, 0.02, base.shape).astype(np.float32)
        repair = base + rs.normal(0, 0.005, base.shape).astype(np.float32)
        core = self.rects[(3, 3)]
        alpha = ers.build_feather_alpha(core, 16, 12, self.h, self.w)
        comp = ers.feather_composite(delivered, repair, alpha)
        eps = 0.02
        for gy in range(GRID):
            for gx in range(GRID):
                y0, y1, x0, x1 = self.rects[(gy, gx)]
                pre = ssim(base[y0:y1, x0:x1], delivered[y0:y1, x0:x1],
                           channel_axis=-1, data_range=1.0)
                post = ssim(base[y0:y1, x0:x1], comp[y0:y1, x0:x1],
                            channel_axis=-1, data_range=1.0)
                self.assertGreaterEqual(
                    post, pre - eps,
                    f"tile ({gy},{gx}) degraded {pre:.4f} -> {post:.4f}")

    def test_merge_and_margin_rects(self):
        a = self.rects[(3, 3)]
        b = self.rects[(3, 4)]   # adjacent -> must merge with margin 16
        c = self.rects[(0, 0)]   # far away + frame corner -> its own clamped region
        regions = ers.merge_and_margin_rects([a, b, c], 16, self.w, self.h)
        self.assertEqual(len(regions), 2)
        cores = [core for core, _ in regions]
        self.assertIn((a[0], a[1], a[2], b[3]), cores)  # a+b bounding box
        self.assertIn(c, cores)
        for _core, (by0, by1, bx0, bx1) in regions:
            self.assertGreaterEqual(by0, 0)
            self.assertGreaterEqual(bx0, 0)
            self.assertLessEqual(by1, self.h)
            self.assertLessEqual(bx1, self.w)
        # merged borders must be pairwise disjoint (no double render, no interior ramps)
        borders = [brd for _c, brd in regions]
        for i in range(len(borders)):
            for j in range(i + 1, len(borders)):
                ai, bj = borders[i], borders[j]
                overlap = not (ai[1] <= bj[0] or bj[1] <= ai[0]
                               or ai[3] <= bj[2] or bj[3] <= ai[2])
                self.assertFalse(overlap)


# --------------------------------------------------------------------------- #
# 5. Blender border Y-flip                                                      #
# --------------------------------------------------------------------------- #
class BorderYFlipTest(unittest.TestCase):
    def test_round_trip(self):
        rs = np.random.RandomState(3)
        for _ in range(50):
            res_x = int(rs.randint(64, 4097))
            res_y = int(rs.randint(64, 2161))
            x0 = int(rs.randint(0, res_x - 1))
            x1 = int(rs.randint(x0 + 1, res_x + 1))
            y0 = int(rs.randint(0, res_y - 1))
            y1 = int(rs.randint(y0 + 1, res_y + 1))
            mnx, mxx, mny, mxy = ers.numpy_rect_to_blender_border(
                x0, y0, x1, y1, res_x, res_y)
            self.assertTrue(0.0 <= mnx < mxx <= 1.0)
            self.assertTrue(0.0 <= mny < mxy <= 1.0)
            # invert: the normalized bottom-left border recovers the numpy rect exactly
            self.assertEqual(round(mnx * res_x), x0)
            self.assertEqual(round(mxx * res_x), x1)
            self.assertEqual(round(res_y - mxy * res_y), y0)
            self.assertEqual(round(res_y - mny * res_y), y1)

    def test_embedded_script_formulas_in_lockstep(self):
        s = ers.BLENDER_SCENE_SCRIPT
        self.assertIn("scene.render.border_min_x = bx0 / RES_X", s)
        self.assertIn("scene.render.border_max_x = bx1 / RES_X", s)
        self.assertIn("scene.render.border_min_y = (RES_Y - by1) / RES_Y", s)
        self.assertIn("scene.render.border_max_y = (RES_Y - by0) / RES_Y", s)
        self.assertIn("scene.render.use_border = True", s)
        self.assertIn("scene.render.use_crop_to_border = False", s)
        self.assertIn("CX_RENDER_DONE_REGION=", s)


# --------------------------------------------------------------------------- #
# 8. embedded Blender scripts still compile (the way the runner writes them)    #
# --------------------------------------------------------------------------- #
class EmbeddedScriptCompileTest(unittest.TestCase):
    def test_scene_and_probe_scripts_compile(self):
        compile(ers.BLENDER_SCENE_SCRIPT, "cx_stack_scene.py", "exec")
        compile(ers.GPU_PROBE_SCRIPT, "cx_gpu_probe.py", "exec")


# --------------------------------------------------------------------------- #
# 6 + 7. stubbed end-to-end runs: accounting sums + default-off byte-identity   #
#        (fresh module instance per run; Blender/scene/EXR/time all stubbed)     #
# --------------------------------------------------------------------------- #
PLANTED = [(1, 2), (5, 6)]


class _FakeClock:
    """Deterministic perf_counter: every call advances exactly 0.5s, so every timed
    numpy span in the runner charges exactly 0.5s — accounting becomes exact math."""

    def __init__(self, step=0.5):
        self.t = 0.0
        self.step = step

    def perf_counter(self):
        self.t += self.step
        return self.t


def _load_fresh_stack_module(name):
    spec = importlib.util.spec_from_file_location(name, STACK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_fake_run_blender_frame(borders_map):
    def fake(blender_bin, script_path, *, blend, out_exr, res_x, res_y, spp, is_ref,
             frame, nframes, cam_motion, seed, bounces, device_pref, timeout_s,
             adaptive=False, adaptive_thr=0.01, adaptive_min=16, denoiser="none",
             guides=False, light_tree=False, require_gpu=False, borders=None,
             out_pattern=None):
        if borders is not None:
            wall = 12.0 + 2.0 * len(borders)
            paths = []
            for i, rect in enumerate(borders):
                token = f"repair:{frame}:{i}"
                borders_map[token] = tuple(int(v) for v in rect)
                paths.append(token)
            return wall, "GPU/FAKE", paths
        if is_ref:
            return 100.0 + frame, "GPU/FAKE", f"ref:{frame}"
        if res_x <= 8:  # the fixed-overhead calibration render (CALIB_RES=8)
            return 3.0, "GPU/FAKE", f"calib:{frame}"
        if denoiser == "none" and not adaptive:  # the raw selection draft B
            return 5.0 + 0.1 * frame, "GPU/FAKE", f"sel:{frame}:{seed}"
        return 20.0 + frame, "GPU/FAKE", f"anchor:{frame}"
    return fake


def _make_fake_read_exr(borders_map, tile_rects_fn):
    def fake(path, res_x, res_y):
        h, w = res_y, res_x
        base = _base_image(h, w)
        kind = path.split(":")[0]
        rs = np.random.RandomState(zlib.crc32(path.encode()) % (2 ** 31))
        if kind == "ref":
            color = base.copy()
        elif kind == "anchor":
            color = base + rs.normal(0, 0.01, base.shape).astype(np.float32)
            _plant_noise(color, PLANTED, rs, 0.35)
        elif kind == "sel":
            color = base + rs.normal(0, 0.05, base.shape).astype(np.float32)
            _plant_noise(color, PLANTED, rs, 0.5)
        elif kind == "repair":
            x0, y0, x1, y1 = borders_map[path]
            color = np.zeros_like(base)
            color[y0:y1, x0:x1] = (
                base[y0:y1, x0:x1]
                + rs.normal(0, 0.002, (y1 - y0, x1 - x0, 3)).astype(np.float32))
        else:
            color = base.copy()
        motion = np.zeros((h, w, 2), np.float32)
        depth = np.zeros((h, w), np.float32)
        return color.astype(np.float32), motion, depth, None
    return fake


def run_stubbed(params, module_name="ers_stub"):
    """Run a FRESH exp_render_stack module instance end-to-end with Blender, the scene
    fetch, the EXR reader and the clock all stubbed deterministically. Returns the
    emitted metrics dict."""
    mod = _load_fresh_stack_module(module_name)
    tmp = tempfile.mkdtemp(prefix="cx_repair_loop_test_")
    mod.WORK_DIR = os.path.join(tmp, "work")
    mod._CACHE_ROOT = os.path.join(tmp, "cache")  # fresh -> always a ref-cache MISS
    mod.log = lambda *a: None
    mod.ensure_system_libs = lambda: None
    mod.ensure_pydeps = lambda: None
    mod.ensure_blender = lambda url: "/fake/blender"
    mod.resolve_scene = lambda s: ("/fake/scene.blend", "classroom", "")
    mod.require_gpu_probe = lambda *a, **k: "GPU/FAKE"
    borders_map = {}
    mod.run_blender_frame = _make_fake_run_blender_frame(borders_map)
    mod.read_exr_layers = _make_fake_read_exr(borders_map, mod._tile_rects)
    mod.time = types.SimpleNamespace(perf_counter=_FakeClock(0.5).perf_counter)
    captured = {}
    mod.emit = lambda obj: captured.setdefault("metrics", obj)
    old_argv = sys.argv
    sys.argv = ["exp_render_stack.py", json.dumps(params)]
    try:
        mod.main()
    finally:
        sys.argv = old_argv
    return captured["metrics"]


BASE_PARAMS = {
    "frames": 3,
    "keyframe_every": 1,       # all-anchor (the RUN 3 / repair-target mode)
    "resolution": "128x128",
    "draft_spp": 512,
    "ref_spp": 4096,
    "denoiser": "oidn",
    "seed": 0,
}


class StubbedRunTest(unittest.TestCase):
    def test_default_off_is_byte_identical_and_legacy_shaped(self):
        m_default = run_stubbed(dict(BASE_PARAMS), "ers_stub_default")
        m_explicit = run_stubbed(dict(BASE_PARAMS, repair_enabled=False),
                                 "ers_stub_explicit_off")
        self.assertEqual(json.dumps(m_default), json.dumps(m_explicit),
                         "explicit repair_enabled=false must be byte-identical to {}")
        # EXACTLY the frozen legacy key set — no repair key leaks into legacy receipts
        self.assertEqual(list(m_default.keys()), LEGACY_METRICS_KEYS)
        for k in m_default:
            self.assertFalse(k.startswith(("repair", "selection", "selector")), k)
        self.assertNotIn("repair", m_default["note"].lower())
        # legacy accounting: anchors (21+22+23) + calibration (3.0); T_ref = 101+102+103
        self.assertAlmostEqual(m_default["T_stack_s"], 21 + 22 + 23 + 3.0, places=4)
        self.assertAlmostEqual(m_default["T_ref_s"], 101 + 102 + 103, places=4)
        self.assertAlmostEqual(m_default["net_speedup"],
                               round(306.0 / 69.0, 4), places=4)
        self.assertFalse(m_default["modeled"])

    def test_repair_on_accounting_sums_every_charged_stage(self):
        m = run_stubbed(dict(BASE_PARAMS, repair_enabled=True, repair_top_k=2,
                             repair_max_per_frame=2, selection_draft_spp=64,
                             repair_margin_px=16, repair_feather_px=12),
                        "ers_stub_repair_on")
        # decomposition fields are internally exact
        self.assertAlmostEqual(
            m["selection_cost_s"],
            round(sum(m["per_frame_selection_draft_s"])
                  + sum(m["per_frame_selection_scoring_s"]), 4), places=4)
        self.assertAlmostEqual(
            m["repair_cost_s"],
            round(sum(m["per_frame_repair_render_s"])
                  + sum(m["per_frame_repair_composite_s"]), 4), places=4)
        self.assertAlmostEqual(m["repair_total_s"],
                               round(m["selection_cost_s"] + m["repair_cost_s"], 4),
                               places=4)
        # T_stack == anchors + calibration + selection + repair (NOTHING uncharged,
        # nothing double-charged; repair_total_s is inside T_stack, not re-added)
        self.assertAlmostEqual(
            m["T_stack_s"],
            round(sum(m["per_keyframe_render_s"]) + m["fixed_overhead_s"]
                  + m["repair_total_s"], 4), places=4)
        # known fake walls: selection drafts 5.1+5.2+5.3, scoring 3 x 0.5
        self.assertAlmostEqual(m["selection_cost_s"], 15.6 + 1.5, places=4)
        # exactly the budgeted tiles were repaired, all from the planted hot set
        self.assertEqual(m["repaired_tile_count"], 2)
        repaired = {(f, tuple(t)) for f, tiles in
                    enumerate(m["repaired_tile_indices"]) for t in tiles}
        for _f, tile in repaired:
            self.assertIn(tile, {tuple(p) for p in PLANTED})
        # net_speedup stays ONE ratio: T_ref / T_stack
        self.assertAlmostEqual(m["net_speedup"],
                               round(m["T_ref_s"] / m["T_stack_s"], 4), places=4)
        # repair adds NO modeled term (kf=1 all-anchor)
        self.assertFalse(m["modeled"])
        # measurement-only post-hoc: every repaired tile improved vs its pre score
        for entry in m["repaired_tile_ssim_after"]:
            self.assertIsNotNone(entry["ssim_pre"])
            self.assertIsNotNone(entry["ssim_after"])
            self.assertGreater(entry["ssim_after"], entry["ssim_pre"])
        # the derived repair knobs resolved as documented (4 x draft, thr/2)
        self.assertEqual(m["repair_spp"], 2048)
        self.assertAlmostEqual(m["repair_adaptive_threshold"], 0.005, places=9)

    def test_selection_charged_even_at_zero_repairs(self):
        m = run_stubbed(dict(BASE_PARAMS, repair_enabled=True, repair_top_k=2,
                             repair_min_divergence=10.0),  # impossible floor
                        "ers_stub_zero_repairs")
        self.assertEqual(m["repaired_tile_count"], 0)
        self.assertEqual(m["repaired_tile_indices"], [[], [], []])
        self.assertAlmostEqual(m["repair_cost_s"], 0.0, places=4)
        self.assertGreater(m["selection_cost_s"], 0.0)
        # the selection draft is charged even when it buys zero repairs
        self.assertAlmostEqual(
            m["T_stack_s"],
            round(sum(m["per_keyframe_render_s"]) + m["fixed_overhead_s"]
                  + m["selection_cost_s"], 4), places=4)

    def test_selector_recall_reported_when_tiles_fail_pre_repair(self):
        m = run_stubbed(dict(BASE_PARAMS, repair_enabled=True, repair_top_k=6,
                             repair_max_per_frame=2), "ers_stub_recall")
        # planted tiles grade badly pre-repair on every frame -> recall is defined
        self.assertIsNotNone(m["selector_recall"])
        self.assertGreater(m["selector_recall"], 0.0)
        self.assertLessEqual(m["selector_recall"], 1.0)
        self.assertEqual(len(m["per_frame_worst_tile_ssim_pre_repair"]), 3)

    def test_bad_selector_fails_loudly(self):
        with self.assertRaises(RuntimeError):
            run_stubbed(dict(BASE_PARAMS, repair_selector="oracle"),
                        "ers_stub_bad_selector")


# --------------------------------------------------------------------------- #
# 9. adapter: repair-carrying metrics -> real per-tile fractions                 #
# --------------------------------------------------------------------------- #
class AdapterRepairPathTest(unittest.TestCase):
    def _metrics(self, **over):
        m = {
            "T_ref_s": 4546.76, "T_stack_s": 1100.0, "quality": 0.985,
            "worst_tile_ssim": 0.952, "p5_tile_ssim": 0.968, "modeled": False,
            "reproject_accept_frac": 1.0, "mean_disoccluded_frac": 0.0,
            "fixed_overhead_s": 4.5, "net_speedup": 4.13, "frames": 4,
            "repair_total_s": 280.0, "repaired_tile_count": 12,
            "selection_cost_s": 120.0, "repair_cost_s": 160.0,
            "repaired_tile_indices": [[[0, 3]], [[1, 2]], [], []],
            "selector_recall": 1.0,
        }
        m.update(over)
        return m

    def test_repair_receipt_uses_real_tile_fractions_no_double_charge(self):
        rec = rsa.RenderSpecAdapter().from_stack_metrics(self._metrics())
        self.assertEqual(rec.units, 4 * 64)
        self.assertEqual(rec.repaired_units, 12)
        self.assertEqual(rec.accepted_units, 4 * 64 - 12)
        self.assertAlmostEqual(rec.repair_cost, 280.0)          # REAL, not the O stand-in
        self.assertAlmostEqual(rec.draft_cost, 1100.0 - 280.0)
        self.assertAlmostEqual(rec.total_product_time, 1100.0)  # == T_stack, no re-add
        self.assertAlmostEqual(rec.speedup_vs_baseline, 4546.76 / 1100.0, places=6)
        self.assertEqual(rec.evidence, rsa.MEASURED)
        self.assertTrue(rec.delivery_eligible)  # gate passes AND nothing modeled
        d = rec.to_dict()
        rsa.assert_canonical(d)
        self.assertAlmostEqual(d["accepted_fraction"], (256 - 12) / 256, places=6)

    def test_legacy_metrics_path_unchanged(self):
        # the exact fixture of the pre-repair adapter tests -> same numbers out
        metrics = {
            "T_ref_s": 40.0, "T_stack_s": 5.0, "quality": 0.991,
            "worst_tile_ssim": 0.962, "p5_tile_ssim": 0.972, "modeled": True,
            "reproject_accept_frac": 0.93, "mean_disoccluded_frac": 0.07,
            "fixed_overhead_s": 0.8, "net_speedup": 8.0,
        }
        rec = rsa.RenderSpecAdapter().from_stack_metrics(metrics)
        self.assertEqual(rec.units, 1)
        self.assertAlmostEqual(rec.repair_cost, 0.8)
        self.assertAlmostEqual(rec.draft_cost, 4.2)
        self.assertAlmostEqual(rec.total_product_time, 5.0)
        self.assertFalse(rec.delivery_eligible)


if __name__ == "__main__":
    unittest.main()
