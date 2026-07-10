#!/usr/bin/env python3
"""
experiments.py — the spec-lab ladder: GPU plan, pod image, and the ordered rungs.

Each rung is consumed by orchestrator.py:
  id          unique short id (also the ledger/resume key)
  track       A=AR-spec-dec  B=video  C=render  D=orchestrator/cost
  description one line
  runner      pod-side script under pod/ that emits ONE json metrics line
  params      base params passed to the runner (json)
  bar         predicate over the metrics dict → viability decision
  bar_desc    human text for --dry-run
  remediations list of {name, override} tried IN ORDER when the bar misses
              (the aggressive auto-improve: inject a different knob and re-run)
  on_fail     'advance' (default, keep going) | 'stop' (halt the ladder)
  timeout_s   per-attempt wall-clock

The metrics contract every runner obeys (keys are optional; the bar reads what it needs):
  speedup           target-vs-draft-verified throughput ratio (AR)
  acceptance        mean fraction of drafted units accepted (0..1)
  net_speedup       end-to-end speedup INCLUDING draft cost + rejects
  quality           perceptual score where higher=better (SSIM 0..1 / -LPIPS / PSNR/40)
  lossless          bool — output provably == target (AR only)
  cost_usd_per_unit modeled $/unit-of-output on this GPU (for the cost track)
  vs_runpod_ratio   our modeled $/job ÷ RunPod rent-it-yourself $/job (<1 = we win)
  error             present ⇒ the attempt hard-failed (bar auto-rejects)
"""

# ---- infra: cheapest-that-works first, widen if capacity/reachability fails ----
# (gpu_type, cloud) — provision_reachable tries these in order and keeps the first
# one this network can actually SSH to. Fallback is monotonic upward: if a SKU is
# unavailable or unhealthy, do not step down to an older/weaker card just to keep
# the run cheap. Upgrade the attempt instead.
# gpu-provisioning-policy (task #20 rewrite, 2026-07-09): base tier A100 then H100 —
# cheapest first, then availability, COMMUNITY then SECURE at each rung; if neither
# base is available UPGRADE to H200. NEVER downgrade to L40S/RTX A6000/A40/CPU.
# Blackwell (B200/B300, sm_100/sm_120) stays out until a Blackwell-capable Blender is
# proven on-box (Blender 4.2 ships no kernels — silent CPU fallback burned $0.58 on
# 2026-07-09). H100/H200 (sm_90) carry a first-render PTX-JIT caveat: give the
# functional GPU probe JIT headroom via the runner param gpu_probe_timeout_s (see
# run_integrated_production_benchmark.py; 2026-07-09 two-pod H100 probe-timeout
# evidence).
GPU_PLAN = [
    ("NVIDIA A100 80GB PCIe", "COMMUNITY"),
    ("NVIDIA A100 80GB PCIe", "SECURE"),
    ("NVIDIA H100 80GB HBM3", "COMMUNITY"),
    ("NVIDIA H100 80GB HBM3", "SECURE"),
    ("NVIDIA H200", "COMMUNITY"),
    ("NVIDIA H200", "SECURE"),
]
POD_IMAGE = "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"
POD_DISK_GB = 60


def _has(m, *ks):
    return isinstance(m, dict) and "error" not in m and all(k in m for k in ks)


LADDER = [
    # ===================== Track A — AR speculative decode (lossless anchor + generality)
    {
        "id": "A1-ngram", "track": "A",
        "description": "vLLM native n-gram speculative decode vs baseline (the known-good anchor)",
        "runner": "exp_ar_vllm.py",
        "params": {"method": "ngram", "model": "Qwen/Qwen2.5-1.5B-Instruct",
                   "num_spec_tokens": 5, "prompts": 64, "max_tokens": 128},
        "bar": lambda m: _has(m, "speedup") and m["speedup"] >= 1.5 and m.get("lossless", True),
        "bar_desc": "speedup ≥ 1.5x AND lossless",
        "remediations": [
            {"name": "spec3", "override": {"num_spec_tokens": 3}},
            {"name": "spec8", "override": {"num_spec_tokens": 8}},
            {"name": "suffix8", "override": {"method": "suffix", "num_spec_tokens": 8}},
            {"name": "decode-heavy", "override": {"max_tokens": 512, "prompts": 32}},
        ],
        "timeout_s": 1800,
    },
    {
        "id": "A2-draft", "track": "A",
        "description": "vLLM draft-model (EAGLE-style) speculative decode — stronger acceptance",
        "runner": "exp_ar_vllm.py",
        "params": {"method": "draft_model", "model": "Qwen/Qwen2.5-1.5B-Instruct",
                   "draft": "Qwen/Qwen2.5-0.5B-Instruct", "num_spec_tokens": 5,
                   "prompts": 64, "max_tokens": 128},
        "bar": lambda m: _has(m, "speedup") and m["speedup"] >= 1.8,
        "bar_desc": "speedup ≥ 1.8x with a real draft model",
        "remediations": [
            {"name": "spec4", "override": {"num_spec_tokens": 4}},
            {"name": "fallback-ngram", "override": {"method": "ngram"}},
        ],
        "timeout_s": 2400,
    },
    {
        "id": "A5-vllm-suffix-upgrade", "track": "A",
        "description": "vLLM suffix speculative decode on a version-selected wheel",
        "runner": "exp_ar_vllm.py",
        "params": {"method": "suffix", "model": "Qwen/Qwen2.5-1.5B-Instruct",
                   "num_spec_tokens": 8, "prompts": 64, "max_tokens": 128},
        "bar": lambda m: _has(m, "speedup") and m["speedup"] >= 1.3 and m.get("lossless", True),
        "bar_desc": "speedup >= 1.3x AND lossless on version-selected vLLM",
        "remediations": [
            {"name": "suffix4", "override": {"num_spec_tokens": 4}},
            {"name": "suffix16", "override": {"num_spec_tokens": 16}},
            {"name": "suffix-decode-heavy", "override": {"max_tokens": 512, "prompts": 32}},
        ],
        "timeout_s": 2400,
    },
    {
        "id": "A6-vllm-draft-upgrade", "track": "A",
        "description": "vLLM draft-model speculative decode on a version-selected wheel",
        "runner": "exp_ar_vllm.py",
        "params": {"method": "draft_model", "model": "Qwen/Qwen2.5-1.5B-Instruct",
                   "draft": "Qwen/Qwen2.5-0.5B-Instruct", "num_spec_tokens": 5,
                   "prompts": 64, "max_tokens": 128},
        "bar": lambda m: _has(m, "speedup") and m["speedup"] >= 1.3 and m.get("lossless", True),
        "bar_desc": "speedup >= 1.3x AND lossless with a real draft model on version-selected vLLM",
        "remediations": [
            {"name": "spec4", "override": {"num_spec_tokens": 4}},
            {"name": "decode-heavy", "override": {"max_tokens": 512, "prompts": 32}},
        ],
        "timeout_s": 3000,
    },
    {
        "id": "A3-bytes", "track": "A",
        "description": "byte-level speculative decode over ARBITRARY files (text/image/audio/binary) — the generality test",
        "runner": "exp_bytes_specdec.py",
        "params": {"files": ["text", "image", "audio", "binary"], "draft_ctx": 8, "n_bytes": 4096},
        "bar": lambda m: _has(m, "acceptance_nontext") and m["acceptance_nontext"] > 0.15,
        "bar_desc": "draft acceptance > 0.15 on NON-text bytes (proves 'any file' predictability)",
        "remediations": [
            {"name": "bigger-ctx", "override": {"draft_ctx": 16}},
            {"name": "order2-ngram", "override": {"draft_order": 2}},
        ],
        "on_fail": "advance",
        "timeout_s": 1200,
    },
    {
        "id": "A4-entropy", "track": "A",
        "description": "acceptance-vs-entropy sweep — WHERE speculation pays (the predictability threshold)",
        "runner": "exp_bytes_specdec.py",
        "params": {"mode": "entropy_sweep", "levels": [0.1, 0.3, 0.5, 0.7, 0.9]},
        "bar": lambda m: _has(m, "entropy_threshold"),
        "bar_desc": "produce a real acceptance-vs-entropy curve + the break-even threshold",
        "remediations": [],
        "on_fail": "advance",
        "timeout_s": 900,
    },

    # ===================== Track B — video frame speculation (the KEYSTONE use case)
    {
        "id": "B1-interp", "track": "B",
        "description": "frame-interpolation draft + full-decode verify, SSIM-gated — net speedup on ordinary footage",
        "runner": "exp_video_interp.py",
        "params": {"clip": "talking_head", "spec_frames": 2, "ssim_gate": 0.97},
        "bar": lambda m: _has(m, "net_speedup", "quality") and m["net_speedup"] >= 1.3 and m["quality"] >= 0.95,
        "bar_desc": "net speedup ≥ 1.3x at SSIM ≥ 0.95 on typical footage",
        "remediations": [
            {"name": "1-spec-frame", "override": {"spec_frames": 1}},
            {"name": "looser-gate", "override": {"ssim_gate": 0.94}},
            {"name": "flow-warp-draft", "override": {"draft": "optical_flow"}},
        ],
        "timeout_s": 2400,
    },
    {
        "id": "B2-residual", "track": "B",
        "description": "motion-compensated RESIDUAL rendering ('render the delta / P-frame' — the owner's 'render bits')",
        "runner": "exp_video_residual.py",
        "params": {"clip": "talking_head", "residual_quant": 8},
        "bar": lambda m: _has(m, "net_speedup") and m["net_speedup"] >= 1.5,
        "bar_desc": "residual-only render ≥ 1.5x cheaper than full-frame at tier quality",
        "remediations": [
            {"name": "coarser-residual", "override": {"residual_quant": 12}},
            {"name": "codec-motion-vectors", "override": {"motion": "from_container"}},
        ],
        "timeout_s": 2400,
    },
    {
        "id": "B3-failguard", "track": "B",
        "description": "scene-cut / fast-motion failure characterization + a CHEAP reject gate (make rejection nearly free)",
        "runner": "exp_video_interp.py",
        "params": {"clip": "high_motion", "spec_frames": 2, "ssim_gate": 0.97, "measure_reject_cost": True},
        "bar": lambda m: _has(m, "reject_overhead") and m["reject_overhead"] < 0.15,
        "bar_desc": "a rejected speculation costs < 15% extra vs just rendering the frame",
        "remediations": [
            {"name": "predict-before-draft", "override": {"prefilter": "motion_magnitude"}},
            {"name": "cut-detector", "override": {"prefilter": "scene_cut"}},
        ],
        "timeout_s": 1800,
    },
    {
        "id": "B4-distributed", "track": "B",
        "description": "DISTRIBUTED video: cheap draft frames on a fleet-class node, verify/correct on the GPU — the ComputeExchange edge",
        "runner": "exp_distributed.py",
        "params": {"modality": "video", "draft_where": "cpu", "verify_where": "gpu"},
        "bar": lambda m: _has(m, "distributed_speedup") and m["distributed_speedup"] > 1.0,
        "bar_desc": "draft-on-cheap + verify-on-GPU beats single-node wall-clock",
        "remediations": [{"name": "batch-verify", "override": {"verify_batch": 8}}],
        "on_fail": "advance",
        "timeout_s": 2400,
    },

    # ===================== Track C — 3D / path-traced render speculation ('render bits')
    {
        "id": "C1-denoise", "track": "C",
        "description": "low-spp draft + neural denoise verify vs high-spp reference — quality-vs-compute",
        "runner": "exp_render_denoise.py",
        "params": {"scene": "cornell", "draft_spp": 1, "ref_spp": 512},
        "bar": lambda m: _has(m, "net_speedup", "quality") and m["net_speedup"] >= 2.0 and m["quality"] >= 0.9,
        "bar_desc": "≥ 2x cheaper than reference at LPIPS/SSIM-tier quality ≥ 0.9",
        "remediations": [
            {"name": "4-spp-draft", "override": {"draft_spp": 4}},
            {"name": "oidn", "override": {"denoiser": "oidn"}},
        ],
        "timeout_s": 2400,
    },
    {
        "id": "C2-adaptive", "track": "C",
        "description": "adaptive sampling driven by PREDICTED residual variance — spend samples only where the draft is uncertain",
        "runner": "exp_render_denoise.py",
        "params": {"scene": "cornell", "mode": "adaptive", "budget_frac": 0.25},
        "bar": lambda m: _has(m, "quality") and m["quality"] >= 0.95,
        "bar_desc": "match reference quality at ≤ 25% of the sample budget",
        "remediations": [{"name": "budget-40", "override": {"budget_frac": 0.40}}],
        "on_fail": "advance",
        "timeout_s": 2400,
    },
    {
        "id": "C3-tiles", "track": "C",
        "description": "DISTRIBUTED render: fleet nodes render low-spp tiles, GPU denoises/composites — the render-marketplace shape",
        "runner": "exp_distributed.py",
        "params": {"modality": "render", "draft_where": "cpu", "verify_where": "gpu"},
        "bar": lambda m: _has(m, "distributed_speedup") and m["distributed_speedup"] > 1.0,
        "bar_desc": "tiled draft-on-fleet + GPU verify beats single-GPU wall-clock",
        "remediations": [],
        "on_fail": "advance",
        "timeout_s": 2400,
    },

    # ===================== Track R — the SAME methodology on REAL content (the honest gate)
    {
        "id": "R1-cycles", "track": "R",
        "description": "REAL Blender Cycles path tracing: low-spp + OIDN denoise draft vs high-spp reference",
        "runner": "exp_cycles_render.py",
        "params": {"draft_spp": 16, "ref_spp": 512, "resolution": 256},
        "bar": lambda m: _has(m, "net_speedup", "quality") and m["net_speedup"] >= 1.5 and m["quality"] >= 0.90,
        "bar_desc": "≥1.5x faster real render at SSIM ≥ 0.90 (real ray tracing, not a noise model)",
        "remediations": [
            {"name": "32spp", "override": {"draft_spp": 32}},
            {"name": "64spp", "override": {"draft_spp": 64}},
        ],
        "on_fail": "advance", "timeout_s": 2400,
    },
    {
        "id": "R2-realvideo", "track": "R",
        "description": "frame speculation on REAL footage (Sintel/Big Buck Bunny) — does it survive real motion?",
        "runner": "exp_real_video.py",
        "params": {"spec_frames": 2, "ssim_gate": 0.95, "draft": "optical_flow", "clip": "auto"},
        "bar": lambda m: _has(m, "net_speedup", "quality") and m["net_speedup"] >= 1.2 and m["quality"] >= 0.92,
        "bar_desc": "≥1.2x at SSIM ≥ 0.92 on REAL motion (harder than synthetic pans)",
        "remediations": [
            {"name": "1-frame", "override": {"spec_frames": 1}},
            {"name": "looser-gate", "override": {"ssim_gate": 0.92}},
            {"name": "blend-draft", "override": {"draft": "blend"}},
        ],
        "on_fail": "advance", "timeout_s": 1800,
    },
    {
        "id": "R3-realimage", "track": "R",
        "description": "REAL image speculation: super-res draft/verify + progressive 'render the delta' residual",
        "runner": "exp_real_image.py",
        "params": {"downscale": 4, "jpeg_draft_q": 30, "images": "auto"},
        "bar": lambda m: _has(m, "net_speedup", "quality") and m["net_speedup"] >= 1.5 and m["quality"] >= 0.90,
        "bar_desc": "≥1.5x at SSIM ≥ 0.90 on real images (best of super-res / residual)",
        "remediations": [{"name": "downscale-2x", "override": {"downscale": 2}}],
        "on_fail": "advance", "timeout_s": 1200,
    },
    {
        "id": "R4-transcode", "track": "R",
        "description": "speculative video TRANSCODE on real footage: fast-preset draft, re-encode only failing segments",
        "runner": "exp_video_transcode.py",
        "params": {"gate": 0.97, "segments": 6, "draft_preset": "ultrafast", "ref_preset": "slow"},
        "bar": lambda m: _has(m, "net_speedup", "quality") and m["net_speedup"] >= 1.3 and m["quality"] >= 0.95,
        "bar_desc": "≥1.3x transcode speedup at SSIM ≥ 0.95 (cheap encode + selective re-do)",
        "remediations": [
            {"name": "looser-gate", "override": {"gate": 0.95}},
            {"name": "more-segments", "override": {"segments": 10}},
        ],
        "on_fail": "advance", "timeout_s": 2400,
    },

    # ===================== Track X — CUSTOM 3D-render speculation (our IP, not framework built-ins)
    {
        "id": "X1-production", "track": "X",
        "description": "CX-Foveate: heavy production scene, our saliency-guided two-pass foveated render (the fair R1 re-test)",
        "runner": "exp_render_production.py",
        "params": {"draft_spp": 48, "ref_spp": 1536, "resolution": 512, "complexity": "heavy"},
        "bar": lambda m: _has(m, "net_speedup", "quality") and m["net_speedup"] >= 2.0 and m["quality"] >= 0.95,
        "bar_desc": "≥2x at SSIM ≥ 0.95 on a heavy scene where the ref render is genuinely slow",
        "remediations": [
            {"name": "ref3072", "override": {"ref_spp": 3072}},
            {"name": "res720", "override": {"resolution": 720}},
        ],
        "on_fail": "advance", "timeout_s": 2400,
    },
    {
        "id": "X2-tiles", "track": "X",
        "description": "CUSTOM spatial-tile routing: our variance/edge hardness classifier spends samples only on hard tiles",
        "runner": "exp_render_tiles.py",
        "params": {"grid": 4, "draft_spp": 32, "hard_spp": 512, "ref_spp": 512, "hard_frac": 0.3, "resolution": 384, "classifier": "blend"},
        "bar": lambda m: _has(m, "net_speedup", "quality") and m["net_speedup"] >= 1.5 and m["quality"] >= 0.95,
        "bar_desc": "≥1.5x at SSIM ≥ 0.95 via per-tile adaptive sample routing (distributable)",
        "remediations": [
            {"name": "edge-classifier", "override": {"classifier": "edge"}},
            {"name": "fewer-hard", "override": {"hard_frac": 0.2}},
        ],
        "on_fail": "advance", "timeout_s": 2400,
    },
    {
        "id": "X3-gbuffer", "track": "X",
        "description": "CUSTOM bounce-hybrid: cheap direct-light draft + full-GI only in GI-heavy regions",
        "runner": "exp_render_gbuffer.py",
        "params": {"draft_bounces": 1, "ref_bounces": 8, "spp": 256, "resolution": 384, "gi_region_frac": 0.35},
        "bar": lambda m: _has(m, "net_speedup", "quality") and m["net_speedup"] >= 1.5 and m["quality"] >= 0.93,
        "bar_desc": "≥1.5x at SSIM ≥ 0.93 by paying full GI only where it matters",
        "remediations": [
            {"name": "0-bounce-draft", "override": {"draft_bounces": 0}},
            {"name": "bigger-gi", "override": {"gi_region_frac": 0.5}},
        ],
        "on_fail": "advance", "timeout_s": 2400,
    },
    {
        "id": "X4-temporal", "track": "X",
        "description": "CUSTOM temporal reuse: render one keyframe, reproject the rest by motion vectors, re-render only disocclusions (render-the-delta for animation)",
        "runner": "exp_render_temporal.py",
        "params": {"frames": 8, "keyframe_every": 8, "spp": 256, "resolution": 384, "disocclusion_thresh": 0.1},
        "bar": lambda m: _has(m, "net_speedup", "quality") and m["net_speedup"] >= 2.0 and m["quality"] >= 0.95,
        "bar_desc": "≥2x at SSIM ≥ 0.95 on animation (1 full keyframe + cheap reprojected patches)",
        "remediations": [
            {"name": "looser-disoc", "override": {"disocclusion_thresh": 0.2}},
            {"name": "key-every-16", "override": {"keyframe_every": 16, "frames": 16}},
        ],
        "on_fail": "advance", "timeout_s": 2400,
    },

    {
        "id": "X5-heavy", "track": "X",
        "description": "DECISIVE: GI-dominated Cornell box where full render is genuinely slow — does bounce-hybrid win in its real regime?",
        "runner": "exp_render_heavy.py",
        "params": {"resolution": 512, "spp": 512, "draft_bounces": 1, "ref_bounces": 24, "gi_region_frac": 0.4, "target_ref_seconds": 120},
        "bar": lambda m: _has(m, "net_speedup", "quality", "gi_cost_ratio") and m["net_speedup"] >= 1.5 and m["quality"] >= 0.90 and m["gi_cost_ratio"] >= 2.0,
        "bar_desc": "≥1.5x at SSIM ≥ 0.90 AND gi_cost_ratio ≥ 2x (GI genuinely expensive — the real regime)",
        "remediations": [
            {"name": "deeper-gi", "override": {"ref_bounces": 32, "target_ref_seconds": 180}},
            {"name": "bigger-region", "override": {"gi_region_frac": 0.55}},
        ],
        "on_fail": "advance", "timeout_s": 3600,
    },

    # ===================== Track D — the general orchestrator + the PRICE proof
    {
        "id": "D1-protocol", "track": "D",
        "description": "one draft/verify/gate protocol drives ALL modalities (a smoke test that the interface is truly general)",
        "runner": "exp_protocol.py",
        "params": {"modalities": ["ar", "bytes", "video", "render"]},
        "bar": lambda m: _has(m, "modalities_ok") and m["modalities_ok"] >= 3,
        "bar_desc": "the same protocol object runs ≥ 3 modalities end-to-end",
        "remediations": [],
        "on_fail": "advance",
        "timeout_s": 1200,
    },
    {
        "id": "D2-costmodel", "track": "D",
        "description": "COST: modeled $/job for speculative-on-fleet vs RunPod rent-it-yourself for the SAME output (the price-out-the-competition proof)",
        "runner": "exp_cost_model.py",
        "params": {"jobs": ["video_render_10min", "batch_infer_10k", "path_trace_scene"]},
        "bar": lambda m: _has(m, "vs_runpod_ratio") and m["vs_runpod_ratio"] < 0.85,
        "bar_desc": "our modeled $/job < 0.85x the DIY-on-RunPod cost for the same output",
        "remediations": [
            {"name": "include-fleet-idle-discount", "override": {"fleet_cost_model": "idle_marginal"}},
        ],
        "on_fail": "advance",
        "timeout_s": 600,
    },
    {
        "id": "D3-endtoend", "track": "D",
        "description": "product demo: quote a video-render job, run it speculatively, prove the receipt is cheaper than the DIY-RunPod cost",
        "runner": "exp_cost_model.py",
        "params": {"mode": "receipt_demo", "job": "video_render_10min"},
        "bar": lambda m: _has(m, "cheaper_than_diy") and m["cheaper_than_diy"],
        "bar_desc": "the delivered receipt beats DIY-on-cloud on total $ for the same output",
        "remediations": [],
        "on_fail": "advance",
        "timeout_s": 900,
    },
]
