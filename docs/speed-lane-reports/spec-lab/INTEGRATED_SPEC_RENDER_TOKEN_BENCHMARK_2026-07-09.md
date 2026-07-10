# Integrated Speculative Render And Token Benchmark

This is one job-level accounting record. Token/control speculation and pixel/render speculation are sequential parts of the same delivery workflow; their component ratios are not multiplied.

```json
{
  "baseline_total_s": 2832.466067,
  "claim_scope": "Measured end-to-end job ratio only; component speedups are not multiplied.",
  "decision": {
    "action": "prune",
    "reason": "render output failed the delivery quality gate"
  },
  "end_to_end_speedup_x": 2.703023,
  "evidence_type": "same_gpu_production_cycles_stack",
  "global_ssim": 0.9855,
  "gpu": {
    "cloud": "SECURE",
    "cuda_driver_version": 0,
    "gpu": "NVIDIA H100 80GB HBM3"
  },
  "job": {
    "frames": 4,
    "job_id": "cx-production-4k-video",
    "render_policy": "motion/depth draft->verify->accept/refine/fallback",
    "resolution": "3840x2160",
    "scene": "classroom",
    "token_policy": "json-template job-event draft->byte verify->repair",
    "workload": "production_video"
  },
  "render_baseline_s": 2832.4645,
  "render_metrics": {
    "T_ref_s": 2832.4645,
    "T_stack_s": 1047.6049,
    "adaptive_min_samples": 16,
    "adaptive_threshold": 0.02,
    "bounces": 12,
    "cam_motion": 1.0,
    "denoise_guides": true,
    "denoiser": "oidn",
    "device": "GPU/OPTIX",
    "disocclusion_thresh": 0.1,
    "draft_spp": 512,
    "fixed_overhead_s": 12.5404,
    "frames": 4,
    "hole_fill": "rerender",
    "keyframe_every": 1,
    "keyframe_indices": [
      0,
      1,
      2,
      3
    ],
    "keyframes": 4,
    "light_tree": true,
    "mean_disoccluded_frac": 0.0,
    "mean_keyframe_pixel_trace_s": 139.7048,
    "mean_keyframe_render_s": 152.2453,
    "modeled": false,
    "net_speedup": 2.7038,
    "note": "KEYSTONE compound end-to-end render stack on ANIMATED 'classroom' (3840x2160, 4 frames, keyframe_every=1, bounces=12). Camera DOLLIED+PANNED+YAWED (cam_motion=1.0) for real screen-space motion + silhouette disocclusion. REFERENCE = every frame FULLY at 4096 spp, adaptive OFF, denoise OFF (true frames); T_ref = summed whole-subprocess wall-clock. OURS = keyframes rendered with the FULL anchor stack [adaptive sampling (thr=0.02, min=16) + oidn denoiser + albedo/normal prefiltered guides + light-tree many-light importance sampling, draft_spp=512] at full whole-subprocess wall-clock; non-key frames reprojected by Cycles motion vectors (our backward-gather warp) + OUR disocclusion mask [OOB|MV-divergence|depth-discontinuity|fwd/bwd-consistency, dilated], disocclusions handled by hole_fill=rerender. net_speedup = T_ref / T_stack = ONE measured wall-clock ratio on the SAME box (NOT a product of per-stage speedups). quality = end-to-end SSIM of DELIVERED frames vs TRUE frames (GLOBAL + per-8x8-tile worst/p5, tonemapped linear HDR) \u2014 real scikit-image on real pixels; SSIM is measurement-only (not charged to T_stack). Every keyframe/reference render TIME is real whole-subprocess wall-clock; every reprojected frame's numpy warp/mask/fill time is real measured wall-time. ONE MODELED STEP: the disocclusion crop re-render is NOT border-rendered per-frame; its cost is charged as fixed_overhead + disocc_frac*keyframe_pixel_trace, where fixed_overhead=12.540s is the MEASURED Blender start+.blend load+BVH cost a real crop pays in FULL (NOT scaled by area) and keyframe_pixel_trace = keyframe_wall - fixed_overhead. Honest and CONSERVATIVE (a resident-Blender pipeline amortizes the fixed overhead across crops) \u2014 NOT an upper bound. Because this crop-trace time is DERIVED (area-scaled from a measured full trace) it is the ONLY non-directly-measured number, so modeled=false. The disocclusion PATCH pixels are the ANCHOR-QUALITY render of that frame (adaptive+denoiser+guides+light-tree, draft_spp) \u2014 exactly what a crop render at the SAME anchor stack produces \u2014 NOT the 4096-spp reference, so the delivered-frame SSIM is a faithful measure of the composited pipeline (no patch scores SSIM~1 by construction). Each non-key frame's anchor render is a QUALITY/motion input only; its FULL cost is NOT charged (only the crop fraction is), so we render more pixels than the pipeline pays for purely to keep the patch quality honest. REPAIR PASS (reference-free): a second RAW draft per frame (64spp, adaptive OFF, denoiser OFF, seed+7919) scored per-tile divergence vs the delivered frame (two-independent-estimate / Noise2Noise selection \u2014 the selector NEVER reads the reference; SSIM-vs-reference stays measurement-only, computed after delivery); the top-12 tiles shot-wide (max 8/frame) were re-rendered with the SAME anchor stack at 2048spp cap + adaptive_thr=0.01 via REAL Cycles border renders (margin 16px) and feather-composited (outer 12px linear ramp; the graded tile gets pure repair pixels). EVERY repair second is real measured wall-clock charged into T_stack: selection drafts + divergence scoring (172.564s) and repair renders + compositing (253.519s) \u2014 charged even when zero tiles are selected. The repair pass adds NO modeled term. KNOWN LIMIT: divergence detects variance, not a denoiser bias shared across seeds; selector_recall (measurement-only) quantifies missed failing tiles each run.",
    "p5_tile_ssim": 0.9664,
    "per_frame_global_ssim": [
      0.9849,
      0.9852,
      0.9857,
      0.986
    ],
    "per_frame_ref_s": [
      702.7596,
      707.0706,
      709.1631,
      713.4712
    ],
    "per_frame_repair_composite_s": [
      11.1522,
      11.6888,
      10.3408,
      12.0724
    ],
    "per_frame_repair_render_s": [
      37.4025,
      56.8314,
      55.8812,
      58.1499
    ],
    "per_frame_selection_draft_s": [
      42.5503,
      38.6091,
      39.729,
      38.8254
    ],
    "per_frame_selection_scoring_s": [
      3.4813,
      3.2556,
      3.2513,
      2.8622
    ],
    "per_frame_worst_tile_ssim": [
      0.9095,
      0.917,
      0.9276,
      0.938
    ],
    "per_frame_worst_tile_ssim_pre_repair": [
      0.9095,
      0.9169,
      0.9275,
      0.9379
    ],
    "per_keyframe_render_s": [
      150.7493,
      149.5658,
      151.7758,
      156.8902
    ],
    "quality": 0.9855,
    "ref_cache_hit": false,
    "ref_spp": 4096,
    "repair_adaptive_threshold": 0.01,
    "repair_cost_s": 253.5193,
    "repair_enabled": true,
    "repair_feather_px": 12,
    "repair_margin_px": 16,
    "repair_max_per_frame": 8,
    "repair_min_divergence": 0.0,
    "repair_seed_offset": 0,
    "repair_selector": "two_draft",
    "repair_spp": 2048,
    "repair_spp_multiplier": 4.0,
    "repair_top_k": 12,
    "repair_total_s": 426.0835,
    "repaired_tile_count": 12,
    "repaired_tile_indices": [
      [
        [
          0,
          5
        ],
        [
          1,
          5
        ]
      ],
      [
        [
          0,
          5
        ],
        [
          0,
          6
        ],
        [
          1,
          5
        ]
      ],
      [
        [
          0,
          5
        ],
        [
          0,
          6
        ],
        [
          1,
          5
        ]
      ],
      [
        [
          0,
          5
        ],
        [
          0,
          6
        ],
        [
          1,
          5
        ],
        [
          1,
          6
        ]
      ]
    ],
    "repaired_tile_ssim_after": [
      {
        "divergence": 0.476939,
        "frame": 0,
        "ssim_after": 0.98,
        "ssim_pre": 0.9793,
        "tile": [
          0,
          5
        ]
      },
      {
        "divergence": 0.475597,
        "frame": 3,
        "ssim_after": 0.9809,
        "ssim_pre": 0.9802,
        "tile": [
          0,
          5
        ]
      },
      {
        "divergence": 0.475449,
        "frame": 2,
        "ssim_after": 0.9806,
        "ssim_pre": 0.98,
        "tile": [
          0,
          5
        ]
      },
      {
        "divergence": 0.474906,
        "frame": 1,
        "ssim_after": 0.9803,
        "ssim_pre": 0.9795,
        "tile": [
          0,
          5
        ]
      },
      {
        "divergence": 0.418072,
        "frame": 0,
        "ssim_after": 0.9785,
        "ssim_pre": 0.9773,
        "tile": [
          1,
          5
        ]
      },
      {
        "divergence": 0.413663,
        "frame": 1,
        "ssim_after": 0.9792,
        "ssim_pre": 0.978,
        "tile": [
          1,
          5
        ]
      },
      {
        "divergence": 0.411038,
        "frame": 3,
        "ssim_after": 0.9705,
        "ssim_pre": 0.9699,
        "tile": [
          0,
          6
        ]
      },
      {
        "divergence": 0.406888,
        "frame": 2,
        "ssim_after": 0.9802,
        "ssim_pre": 0.9789,
        "tile": [
          1,
          5
        ]
      },
      {
        "divergence": 0.404138,
        "frame": 3,
        "ssim_after": 0.9812,
        "ssim_pre": 0.9799,
        "tile": [
          1,
          5
        ]
      },
      {
        "divergence": 0.40033,
        "frame": 2,
        "ssim_after": 0.9708,
        "ssim_pre": 0.9702,
        "tile": [
          0,
          6
        ]
      },
      {
        "divergence": 0.385192,
        "frame": 1,
        "ssim_after": 0.9716,
        "ssim_pre": 0.9711,
        "tile": [
          0,
          6
        ]
      },
      {
        "divergence": 0.375783,
        "frame": 3,
        "ssim_after": 0.9703,
        "ssim_pre": 0.969,
        "tile": [
          1,
          6
        ]
      }
    ],
    "reproject_accept_frac": 1.0,
    "reprojected_frames": 0,
    "requested_scene": "classroom",
    "resolution": "3840x2160",
    "scene": "classroom",
    "selection_cost_s": 172.5641,
    "selection_draft_spp": 64,
    "selection_seed_offset": 7919,
    "selector_recall": 0.0,
    "selector_scores": {
      "per_frame_max": [
        0.476939,
        0.474906,
        0.475449,
        0.475597
      ],
      "per_frame_p95": [
        0.327297,
        0.340917,
        0.351172,
        0.368145
      ],
      "selected": [
        {
          "divergence": 0.476939,
          "frame": 0,
          "tile": [
            0,
            5
          ]
        },
        {
          "divergence": 0.475597,
          "frame": 3,
          "tile": [
            0,
            5
          ]
        },
        {
          "divergence": 0.475449,
          "frame": 2,
          "tile": [
            0,
            5
          ]
        },
        {
          "divergence": 0.474906,
          "frame": 1,
          "tile": [
            0,
            5
          ]
        },
        {
          "divergence": 0.418072,
          "frame": 0,
          "tile": [
            1,
            5
          ]
        },
        {
          "divergence": 0.413663,
          "frame": 1,
          "tile": [
            1,
            5
          ]
        },
        {
          "divergence": 0.411038,
          "frame": 3,
          "tile": [
            0,
            6
          ]
        },
        {
          "divergence": 0.406888,
          "frame": 2,
          "tile": [
            1,
            5
          ]
        },
        {
          "divergence": 0.404138,
          "frame": 3,
          "tile": [
            1,
            5
          ]
        },
        {
          "divergence": 0.40033,
          "frame": 2,
          "tile": [
            0,
            6
          ]
        },
        {
          "divergence": 0.385192,
          "frame": 1,
          "tile": [
            0,
            6
          ]
        },
        {
          "divergence": 0.375783,
          "frame": 3,
          "tile": [
            1,
            6
          ]
        }
      ]
    },
    "worst_tile_ssim": 0.9095
  },
  "render_modeled": false,
  "render_spec_s": 1047.6049,
  "spec_total_s": 1047.888235,
  "token": {
    "accepted_bytes": 47460,
    "accepted_fraction": 0.9999789300688987,
    "baseline_s": 0.00156729097943753,
    "exact": true,
    "generated_bytes": 47461,
    "proposal_sources": {
      "json_template": 47460
    },
    "spec_s": 0.28333454206585884,
    "target_call_reduction_x": 2791.823529411765,
    "target_calls_baseline": 47461,
    "target_calls_spec": 17
  },
  "token_baseline_s": 0.00156729097943753,
  "token_exact": true,
  "token_spec_s": 0.28333454206585884,
  "worst_tile_ssim": 0.9095
}
```
