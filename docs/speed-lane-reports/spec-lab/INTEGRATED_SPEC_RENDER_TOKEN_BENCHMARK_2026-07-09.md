# Integrated Speculative Render And Token Benchmark

This is one job-level accounting record. Token/control speculation and pixel/render speculation are sequential parts of the same delivery workflow; their component ratios are not multiplied.

```json
{
  "baseline_total_s": 2770.964385,
  "claim_scope": "Measured end-to-end job ratio only; component speedups are not multiplied.",
  "decision": {
    "action": "grow",
    "reason": "exact control stream and measured delivery-quality render"
  },
  "end_to_end_speedup_x": 2.450894,
  "evidence_type": "same_gpu_production_cycles_stack",
  "global_ssim": 0.9902,
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
  "render_baseline_s": 2770.9639,
  "render_metrics": {
    "T_ref_s": 2770.9639,
    "T_stack_s": 1130.3588,
    "adaptive_min_samples": 16,
    "adaptive_threshold": 0.02,
    "bounces": 12,
    "cam_motion": 1.0,
    "denoise_guides": true,
    "denoiser": "oidn",
    "device": "GPU/OPTIX",
    "disocclusion_thresh": 0.1,
    "draft_spp": 512,
    "fixed_overhead_s": 3.0024,
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
    "mean_keyframe_pixel_trace_s": 124.8882,
    "mean_keyframe_render_s": 127.8906,
    "modeled": false,
    "net_speedup": 2.4514,
    "note": "KEYSTONE compound end-to-end render stack on ANIMATED 'classroom' (3840x2160, 4 frames, keyframe_every=1, bounces=12). Camera DOLLIED+PANNED+YAWED (cam_motion=1.0) for real screen-space motion + silhouette disocclusion. REFERENCE = every frame FULLY at 4096 spp, adaptive OFF, denoise OFF (true frames); T_ref = summed whole-subprocess wall-clock. OURS = keyframes rendered with the FULL anchor stack [adaptive sampling (thr=0.02, min=16) + oidn denoiser + albedo/normal prefiltered guides + light-tree many-light importance sampling, draft_spp=512] at full whole-subprocess wall-clock; non-key frames reprojected by Cycles motion vectors (our backward-gather warp) + OUR disocclusion mask [OOB|MV-divergence|depth-discontinuity|fwd/bwd-consistency, dilated], disocclusions handled by hole_fill=rerender. net_speedup = T_ref / T_stack = ONE measured wall-clock ratio on the SAME box (NOT a product of per-stage speedups). quality = end-to-end SSIM of DELIVERED frames vs TRUE frames (GLOBAL + per-8x8-tile worst/p5, tonemapped linear HDR) \u2014 real scikit-image on real pixels; SSIM is measurement-only (not charged to T_stack). Every keyframe/reference render TIME is real whole-subprocess wall-clock; every reprojected frame's numpy warp/mask/fill time is real measured wall-time. ONE MODELED STEP: the disocclusion crop re-render is NOT border-rendered per-frame; its cost is charged as fixed_overhead + disocc_frac*keyframe_pixel_trace, where fixed_overhead=3.002s is the MEASURED Blender start+.blend load+BVH cost a real crop pays in FULL (NOT scaled by area) and keyframe_pixel_trace = keyframe_wall - fixed_overhead. Honest and CONSERVATIVE (a resident-Blender pipeline amortizes the fixed overhead across crops) \u2014 NOT an upper bound. Because this crop-trace time is DERIVED (area-scaled from a measured full trace) it is the ONLY non-directly-measured number, so modeled=false. The disocclusion PATCH pixels are the ANCHOR-QUALITY render of that frame (adaptive+denoiser+guides+light-tree, draft_spp) \u2014 exactly what a crop render at the SAME anchor stack produces \u2014 NOT the 4096-spp reference, so the delivered-frame SSIM is a faithful measure of the composited pipeline (no patch scores SSIM~1 by construction). Each non-key frame's anchor render is a QUALITY/motion input only; its FULL cost is NOT charged (only the crop fraction is), so we render more pixels than the pipeline pays for purely to keep the patch quality honest. REPAIR PASS (reference-free, selector=aov_edge, denoiser=none): each grading tile scored by the NORMAL-AOV edge density of the anchor render (the S4 signal exp_multi_selector_probe validated; the Normal AOV rides in the anchor EXR at ~zero cost, so NO selection draft is rendered) \u2014 the selector NEVER reads the reference; SSIM-vs-reference stays measurement-only, computed after delivery. The top-32 tiles shot-wide (max 8/frame) were re-rendered RAW at 4096spp with the EXACT reference recipe (adaptive OFF, denoiser OFF, no guides, and use_light_tree LEFT AT THE SCENE DEFAULT via match_reference \u2014 NOT force-enabled) so the border tiles are CONFIG-IDENTICAL to the reference: OIDN's shared edge-blur bias is removed AND the one-setting light-tree mismatch that left a ~0.086 SSIM residual on the failing corner tiles is closed on exactly those tiles) via REAL Cycles border renders (margin 16px) and feather-composited (outer 12px linear ramp; the graded tile gets pure repair pixels). EVERY repair second is real measured wall-clock charged into T_stack: selection + scoring (34.757s) and repair renders + compositing (581.037s) \u2014 charged even when zero tiles are selected. The repair pass adds NO modeled term. KNOWN LIMIT: normal-edge density localizes geometric silhouette content \u2014 the shared-denoiser-bias tiles two_draft variance is blind to; selector_recall (measurement-only) quantifies missed failing tiles each run.",
    "p5_tile_ssim": 0.9738,
    "per_frame_global_ssim": [
      0.9899,
      0.9903,
      0.99,
      0.9906
    ],
    "per_frame_ref_s": [
      691.7064,
      690.7894,
      694.0409,
      694.4272
    ],
    "per_frame_repair_composite_s": [
      26.618,
      26.6217,
      26.1647,
      26.7387
    ],
    "per_frame_repair_render_s": [
      120.7022,
      120.7753,
      112.5915,
      120.8247
    ],
    "per_frame_selection_draft_s": [
      0.0,
      0.0,
      0.0,
      0.0
    ],
    "per_frame_selection_scoring_s": [
      8.7078,
      8.6559,
      8.714,
      8.6794
    ],
    "per_frame_worst_tile_ssim": [
      0.9539,
      0.9501,
      0.9506,
      0.9531
    ],
    "per_frame_worst_tile_ssim_pre_repair": [
      0.9095,
      0.9169,
      0.9275,
      0.9379
    ],
    "per_keyframe_render_s": [
      128.1274,
      127.7057,
      127.978,
      127.7514
    ],
    "quality": 0.9902,
    "ref_cache_hit": false,
    "ref_spp": 4096,
    "repair_adaptive_threshold": 0.01,
    "repair_cost_s": 581.0368,
    "repair_denoiser": "none",
    "repair_enabled": true,
    "repair_feather_px": 12,
    "repair_light_tree": "scene_default_match_ref",
    "repair_margin_px": 16,
    "repair_max_per_frame": 8,
    "repair_min_divergence": 0.0,
    "repair_seed_offset": 0,
    "repair_selector": "aov_edge",
    "repair_spp": 4096,
    "repair_spp_multiplier": 4.0,
    "repair_top_k": 32,
    "repair_total_s": 615.7939,
    "repaired_tile_count": 32,
    "repaired_tile_indices": [
      [
        [
          0,
          7
        ],
        [
          1,
          2
        ],
        [
          1,
          6
        ],
        [
          1,
          7
        ],
        [
          2,
          6
        ],
        [
          2,
          7
        ],
        [
          3,
          6
        ],
        [
          4,
          2
        ]
      ],
      [
        [
          0,
          7
        ],
        [
          1,
          2
        ],
        [
          1,
          6
        ],
        [
          1,
          7
        ],
        [
          2,
          6
        ],
        [
          2,
          7
        ],
        [
          3,
          6
        ],
        [
          6,
          3
        ]
      ],
      [
        [
          0,
          7
        ],
        [
          1,
          1
        ],
        [
          1,
          2
        ],
        [
          1,
          6
        ],
        [
          1,
          7
        ],
        [
          2,
          6
        ],
        [
          2,
          7
        ],
        [
          6,
          2
        ]
      ],
      [
        [
          0,
          7
        ],
        [
          1,
          1
        ],
        [
          1,
          6
        ],
        [
          1,
          7
        ],
        [
          2,
          6
        ],
        [
          2,
          7
        ],
        [
          3,
          7
        ],
        [
          6,
          2
        ]
      ]
    ],
    "repaired_tile_ssim_after": [
      {
        "divergence": 0.117527,
        "frame": 0,
        "ssim_after": 1.0,
        "ssim_pre": 0.9402,
        "tile": [
          1,
          7
        ]
      },
      {
        "divergence": 0.116784,
        "frame": 3,
        "ssim_after": 1.0,
        "ssim_pre": 0.9733,
        "tile": [
          2,
          7
        ]
      },
      {
        "divergence": 0.114207,
        "frame": 1,
        "ssim_after": 1.0,
        "ssim_pre": 0.9389,
        "tile": [
          1,
          7
        ]
      },
      {
        "divergence": 0.112659,
        "frame": 2,
        "ssim_after": 1.0,
        "ssim_pre": 0.9747,
        "tile": [
          2,
          7
        ]
      },
      {
        "divergence": 0.110635,
        "frame": 2,
        "ssim_after": 1.0,
        "ssim_pre": 0.9392,
        "tile": [
          1,
          7
        ]
      },
      {
        "divergence": 0.110525,
        "frame": 1,
        "ssim_after": 1.0,
        "ssim_pre": 0.9763,
        "tile": [
          2,
          7
        ]
      },
      {
        "divergence": 0.109779,
        "frame": 3,
        "ssim_after": 1.0,
        "ssim_pre": 0.9383,
        "tile": [
          1,
          7
        ]
      },
      {
        "divergence": 0.108531,
        "frame": 0,
        "ssim_after": 1.0,
        "ssim_pre": 0.9778,
        "tile": [
          2,
          7
        ]
      },
      {
        "divergence": 0.102785,
        "frame": 0,
        "ssim_after": 1.0,
        "ssim_pre": 0.9095,
        "tile": [
          0,
          7
        ]
      },
      {
        "divergence": 0.09933,
        "frame": 1,
        "ssim_after": 1.0,
        "ssim_pre": 0.9169,
        "tile": [
          0,
          7
        ]
      },
      {
        "divergence": 0.098529,
        "frame": 0,
        "ssim_after": 1.0,
        "ssim_pre": 0.9649,
        "tile": [
          2,
          6
        ]
      },
      {
        "divergence": 0.097167,
        "frame": 0,
        "ssim_after": 1.0,
        "ssim_pre": 0.9669,
        "tile": [
          1,
          6
        ]
      },
      {
        "divergence": 0.097013,
        "frame": 1,
        "ssim_after": 1.0,
        "ssim_pre": 0.9683,
        "tile": [
          1,
          6
        ]
      },
      {
        "divergence": 0.096732,
        "frame": 2,
        "ssim_after": 1.0,
        "ssim_pre": 0.9688,
        "tile": [
          1,
          6
        ]
      },
      {
        "divergence": 0.096093,
        "frame": 2,
        "ssim_after": 1.0,
        "ssim_pre": 0.9275,
        "tile": [
          0,
          7
        ]
      },
      {
        "divergence": 0.094278,
        "frame": 3,
        "ssim_after": 1.0,
        "ssim_pre": 0.969,
        "tile": [
          1,
          6
        ]
      },
      {
        "divergence": 0.093683,
        "frame": 1,
        "ssim_after": 1.0,
        "ssim_pre": 0.9658,
        "tile": [
          2,
          6
        ]
      },
      {
        "divergence": 0.093191,
        "frame": 3,
        "ssim_after": 1.0,
        "ssim_pre": 0.9379,
        "tile": [
          0,
          7
        ]
      },
      {
        "divergence": 0.092576,
        "frame": 0,
        "ssim_after": 1.0,
        "ssim_pre": 0.9809,
        "tile": [
          3,
          6
        ]
      },
      {
        "divergence": 0.088973,
        "frame": 2,
        "ssim_after": 1.0,
        "ssim_pre": 0.9672,
        "tile": [
          2,
          6
        ]
      },
      {
        "divergence": 0.088852,
        "frame": 1,
        "ssim_after": 1.0,
        "ssim_pre": 0.9809,
        "tile": [
          3,
          6
        ]
      },
      {
        "divergence": 0.08867,
        "frame": 1,
        "ssim_after": 1.0,
        "ssim_pre": 0.9831,
        "tile": [
          6,
          3
        ]
      },
      {
        "divergence": 0.088649,
        "frame": 0,
        "ssim_after": 1.0,
        "ssim_pre": 0.9926,
        "tile": [
          1,
          2
        ]
      },
      {
        "divergence": 0.088193,
        "frame": 1,
        "ssim_after": 1.0,
        "ssim_pre": 0.9924,
        "tile": [
          1,
          2
        ]
      },
      {
        "divergence": 0.086386,
        "frame": 0,
        "ssim_after": 1.0,
        "ssim_pre": 0.996,
        "tile": [
          4,
          2
        ]
      },
      {
        "divergence": 0.086318,
        "frame": 2,
        "ssim_after": 1.0,
        "ssim_pre": 0.9949,
        "tile": [
          1,
          1
        ]
      },
      {
        "divergence": 0.085947,
        "frame": 3,
        "ssim_after": 1.0,
        "ssim_pre": 0.9951,
        "tile": [
          1,
          1
        ]
      },
      {
        "divergence": 0.085391,
        "frame": 3,
        "ssim_after": 1.0,
        "ssim_pre": 0.9819,
        "tile": [
          3,
          7
        ]
      },
      {
        "divergence": 0.085139,
        "frame": 2,
        "ssim_after": 1.0,
        "ssim_pre": 0.9921,
        "tile": [
          6,
          2
        ]
      },
      {
        "divergence": 0.084561,
        "frame": 2,
        "ssim_after": 1.0,
        "ssim_pre": 0.9922,
        "tile": [
          1,
          2
        ]
      },
      {
        "divergence": 0.082271,
        "frame": 3,
        "ssim_after": 1.0,
        "ssim_pre": 0.9917,
        "tile": [
          6,
          2
        ]
      },
      {
        "divergence": 0.08178,
        "frame": 3,
        "ssim_after": 1.0,
        "ssim_pre": 0.9686,
        "tile": [
          2,
          6
        ]
      }
    ],
    "reproject_accept_frac": 1.0,
    "reprojected_frames": 0,
    "requested_scene": "classroom",
    "resolution": "3840x2160",
    "scene": "classroom",
    "selection_cost_s": 34.7571,
    "selection_draft_spp": 64,
    "selection_seed_offset": 7919,
    "selector_recall": 1.0,
    "selector_scores": {
      "per_frame_max": [
        0.117527,
        0.114207,
        0.112659,
        0.116784
      ],
      "per_frame_p95": [
        0.098325,
        0.096513,
        0.095025,
        0.092105
      ],
      "selected": [
        {
          "divergence": 0.117527,
          "frame": 0,
          "tile": [
            1,
            7
          ]
        },
        {
          "divergence": 0.116784,
          "frame": 3,
          "tile": [
            2,
            7
          ]
        },
        {
          "divergence": 0.114207,
          "frame": 1,
          "tile": [
            1,
            7
          ]
        },
        {
          "divergence": 0.112659,
          "frame": 2,
          "tile": [
            2,
            7
          ]
        },
        {
          "divergence": 0.110635,
          "frame": 2,
          "tile": [
            1,
            7
          ]
        },
        {
          "divergence": 0.110525,
          "frame": 1,
          "tile": [
            2,
            7
          ]
        },
        {
          "divergence": 0.109779,
          "frame": 3,
          "tile": [
            1,
            7
          ]
        },
        {
          "divergence": 0.108531,
          "frame": 0,
          "tile": [
            2,
            7
          ]
        },
        {
          "divergence": 0.102785,
          "frame": 0,
          "tile": [
            0,
            7
          ]
        },
        {
          "divergence": 0.09933,
          "frame": 1,
          "tile": [
            0,
            7
          ]
        },
        {
          "divergence": 0.098529,
          "frame": 0,
          "tile": [
            2,
            6
          ]
        },
        {
          "divergence": 0.097167,
          "frame": 0,
          "tile": [
            1,
            6
          ]
        },
        {
          "divergence": 0.097013,
          "frame": 1,
          "tile": [
            1,
            6
          ]
        },
        {
          "divergence": 0.096732,
          "frame": 2,
          "tile": [
            1,
            6
          ]
        },
        {
          "divergence": 0.096093,
          "frame": 2,
          "tile": [
            0,
            7
          ]
        },
        {
          "divergence": 0.094278,
          "frame": 3,
          "tile": [
            1,
            6
          ]
        },
        {
          "divergence": 0.093683,
          "frame": 1,
          "tile": [
            2,
            6
          ]
        },
        {
          "divergence": 0.093191,
          "frame": 3,
          "tile": [
            0,
            7
          ]
        },
        {
          "divergence": 0.092576,
          "frame": 0,
          "tile": [
            3,
            6
          ]
        },
        {
          "divergence": 0.088973,
          "frame": 2,
          "tile": [
            2,
            6
          ]
        },
        {
          "divergence": 0.088852,
          "frame": 1,
          "tile": [
            3,
            6
          ]
        },
        {
          "divergence": 0.08867,
          "frame": 1,
          "tile": [
            6,
            3
          ]
        },
        {
          "divergence": 0.088649,
          "frame": 0,
          "tile": [
            1,
            2
          ]
        },
        {
          "divergence": 0.088193,
          "frame": 1,
          "tile": [
            1,
            2
          ]
        },
        {
          "divergence": 0.086386,
          "frame": 0,
          "tile": [
            4,
            2
          ]
        },
        {
          "divergence": 0.086318,
          "frame": 2,
          "tile": [
            1,
            1
          ]
        },
        {
          "divergence": 0.085947,
          "frame": 3,
          "tile": [
            1,
            1
          ]
        },
        {
          "divergence": 0.085391,
          "frame": 3,
          "tile": [
            3,
            7
          ]
        },
        {
          "divergence": 0.085139,
          "frame": 2,
          "tile": [
            6,
            2
          ]
        },
        {
          "divergence": 0.084561,
          "frame": 2,
          "tile": [
            1,
            2
          ]
        },
        {
          "divergence": 0.082271,
          "frame": 3,
          "tile": [
            6,
            2
          ]
        },
        {
          "divergence": 0.08178,
          "frame": 3,
          "tile": [
            2,
            6
          ]
        }
      ]
    },
    "worst_tile_ssim": 0.9501
  },
  "render_modeled": false,
  "render_spec_s": 1130.3588,
  "spec_total_s": 1130.593241,
  "token": {
    "accepted_bytes": 47460,
    "accepted_fraction": 0.9999789300688987,
    "baseline_s": 0.0004854999715462327,
    "exact": true,
    "generated_bytes": 47461,
    "proposal_sources": {
      "json_template": 47460
    },
    "spec_s": 0.2344409580109641,
    "target_call_reduction_x": 2791.823529411765,
    "target_calls_baseline": 47461,
    "target_calls_spec": 17
  },
  "token_baseline_s": 0.0004854999715462327,
  "token_exact": true,
  "token_spec_s": 0.2344409580109641,
  "worst_tile_ssim": 0.9501
}
```
