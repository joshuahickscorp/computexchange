#!/usr/bin/env python3
"""
tuning_spaces.py — the parameter search spaces for the autonomous tuning loop.

Each TARGET names a runner (pod/<runner>.py), a fixed base config, a discrete search
space (ordered value lists per knob, so coordinate-ascent has a notion of neighbors),
and the quality tiers we optimize at. The objective everywhere is the same:

    MAXIMIZE net_speedup  SUBJECT TO  quality (SSIM) >= q_floor

optimized separately at each q_floor tier, so the output is a "best speedup at each
quality tier" table (the tiered-quality product) PLUS the full speed/quality Pareto
frontier. Near-lossless (0.98/0.99) is the tier that matters most — the owner's bar.

The metric each runner emits that we read: net_speedup (maximize) and quality (SSIM,
the constraint). A trial with "error" or quality below the floor scores -inf.

The knobs below are the ones the tuning PROGRAM sweeps. Every one is OPTIONAL in its
runner with a safe default that reproduces the original behavior, so:
  * the base config is the proven starting point,
  * OFAT perturbs one knob at a time off that base,
  * coordinate-ascent walks the ordered neighbor lists,
  * old ledger entries stay valid (adding a knob does not change how an old config runs).
See docs/speed-lane-reports/spec-lab/TUNING_PLAN.md for the full experiment program:
the hypothesis and decision metric behind each knob, the combined experiments, and the
loop/stopping methodology.
"""

# Quality tiers to optimize at, hardest (most lossless) first — the owner's "pretty
# much lossless" is the 0.98/0.99 tiers; 0.95 is a "preview" tier for the product.
Q_TIERS = [0.99, 0.98, 0.95, 0.90]

TARGETS = [
    {
        "id": "temporal",
        "runner": "exp_render_temporal.py",
        "timeout_s": 900,
        # Base config; the search perturbs one knob at a time off this. Every added knob
        # is at its OLD-behavior default here, so the base reproduces the proven point.
        "base": {
            "frames": 12, "keyframe_every": 6, "spp": 50, "resolution": 384,
            "disocclusion_thresh": 0.1,
            # new knobs at their backward-compatible defaults:
            "adaptive_keyframe": False, "quality_floor": 0.98,
            "reproject_method": "backward", "hole_fill": "rerender",
            "error_feedback": False, "mv_precision": "full",
        },
        # Ordered value lists (adjacency = neighbor for coordinate ascent).
        "space": {
            # --- the original proven knobs -----------------------------------
            "keyframe_every":     [2, 3, 4, 6, 8, 12, 16, 24],   # fewer keyframes -> more speedup, less quality
            "disocclusion_thresh":[0.02, 0.04, 0.07, 0.10, 0.15, 0.22, 0.30],  # how aggressively to re-render changed regions
            "spp":                [16, 25, 50, 100, 200],        # keyframe/patch sample count
            "resolution":         [384, 512],                    # (compute cost scales; quality of vectors)
            "frames":             [8, 12, 16, 24],               # animation length (more frames amortize the keyframe)
            # --- the near-lossless levers (new) ------------------------------
            # ADAPTIVE keyframing is the key near-lossless lever: insert a fresh keyframe
            # the moment predicted reprojection quality drops below quality_floor.
            "adaptive_keyframe":  [False, True],
            "quality_floor":      [0.90, 0.95, 0.97, 0.98, 0.99],  # only consulted when adaptive_keyframe=True
            # reprojection method: how we warp the keyframe into each frame.
            "reproject_method":   ["backward", "forward_splat", "bidirectional"],
            # hole filling for disoccluded pixels: rerender (true patch, highest quality)
            # vs nearest / inpaint (no re-render, cheaper, lower quality).
            "hole_fill":          ["rerender", "nearest", "inpaint"],
            # error feedback: accumulate reprojection residual and correct drift.
            "error_feedback":     [False, True],
            # motion-vector precision: coarser MVs are cheaper to transmit (distributed
            # variant) — measures the quality cost of quantizing the flow field.
            "mv_precision":       ["full", "half", "int"],
        },
        "q_tiers": Q_TIERS,
        # The knob most likely to trade speed<->quality; OFAT sweeps it densely first.
        "primary_knob": "keyframe_every",
        # The near-lossless product lever the loop should lean on at the 0.98/0.99 tiers.
        "lossless_lever": "adaptive_keyframe",
    },
    {
        "id": "transcode",
        "runner": "exp_video_transcode.py",
        "timeout_s": 900,
        "base": {
            "gate": 0.97, "segments": 8, "draft_preset": "ultrafast", "ref_preset": "slow",
            # new knobs at backward-compatible defaults:
            "draft_crf": 28, "ref_crf": 20, "codec": "libx264",
            "two_pass": False, "scene_aware": False, "hwenc": False,
        },
        "space": {
            # --- the original proven knobs -----------------------------------
            "gate":         [0.90, 0.93, 0.95, 0.97, 0.98, 0.99],   # accept-a-cheap-segment threshold
            "segments":     [4, 6, 8, 12, 16, 24],                  # granularity of selective re-encode
            "draft_preset": ["ultrafast", "superfast", "veryfast", "faster", "fast"],  # cheap encode speed/quality
            "ref_preset":   ["medium", "slow", "slower", "veryslow"],                  # the "final" quality
            # --- CRF pairs (new): the quality/size operating points -----------
            "draft_crf":    [23, 26, 28, 30, 32],   # cheap encode CRF (higher = smaller/faster/worse)
            "ref_crf":      [16, 18, 20, 22],        # ground-truth CRF (lower = better final)
            # --- codec ladder (new): x264 vs x265 vs AV1 vs VP9 --------------
            # A trial falls back to libx264 (with a note) if the encoder is unavailable.
            "codec":        ["libx264", "libx265", "libaom-av1", "libvpx-vp9"],
            # --- 2-pass rate control (new) -----------------------------------
            "two_pass":     [False, True],
            # --- scene-aware segmentation (new): split on real cuts ----------
            "scene_aware":  [False, True],
            "scene_threshold": [0.20, 0.30, 0.40],   # ffmpeg scene-cut sensitivity (only when scene_aware=True)
            # --- hardware encoder (new): NVENC if present on the pod ---------
            "hwenc":        [False, True],
        },
        "q_tiers": Q_TIERS,
        "primary_knob": "gate",
        "lossless_lever": "gate",
    },
]
