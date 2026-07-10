#!/usr/bin/env python3
"""Print the next research queue without launching pods.

This is intentionally non-executing scaffolding: it gives the next agent a stable list of
experiments, timeouts, and pod-side commands while keeping credentials out of files.
"""

import argparse
import json


QUEUE = [
    {
        "id": "analytical_thr_0_01",
        "priority": 1,
        "runner": "exp_render_stack_analytical.py",
        "timeout_s": 2400,
        "why": "Cold-cache threshold push: test whether tighter than 0.02 improves the analytical wall.",
        "config": {
            "frames": 8,
            "keyframe_every": 2,
            "draft_spp": 512,
            "ref_spp": 4096,
            "adaptive_threshold": 0.02,
            "denoiser": "oidn",
            "denoise_guides": True,
            "light_tree": True,
            "hole_fill": "rerender",
            "disocclusion_thresh": 0.01,
            "resolution": "1920x1080",
            "scene": "classroom",
            "bounces": 12,
            "device": "AUTO",
            "depth_convention": "auto",
            "probe_identity": True,
        },
    },
    {
        "id": "analytical_thr_0_005",
        "priority": 2,
        "runner": "exp_render_stack_analytical.py",
        "timeout_s": 1200,
        "why": "Cache-warm threshold push: see if 0.005 continues, plateaus, or reverses.",
        "config": {
            "frames": 8,
            "keyframe_every": 2,
            "draft_spp": 512,
            "ref_spp": 4096,
            "adaptive_threshold": 0.02,
            "denoiser": "oidn",
            "denoise_guides": True,
            "light_tree": True,
            "hole_fill": "rerender",
            "disocclusion_thresh": 0.005,
            "resolution": "1920x1080",
            "scene": "classroom",
            "bounces": 12,
            "device": "AUTO",
            "depth_convention": "auto",
            "probe_identity": True,
        },
    },
    {
        "id": "analytical_thr_0_001",
        "priority": 3,
        "runner": "exp_render_stack_analytical.py",
        "timeout_s": 1200,
        "why": "Cache-warm extreme threshold: tells us whether over-masking helps or starts losing speed/quality.",
        "config": {
            "frames": 8,
            "keyframe_every": 2,
            "draft_spp": 512,
            "ref_spp": 4096,
            "adaptive_threshold": 0.02,
            "denoiser": "oidn",
            "denoise_guides": True,
            "light_tree": True,
            "hole_fill": "rerender",
            "disocclusion_thresh": 0.001,
            "resolution": "1920x1080",
            "scene": "classroom",
            "bounces": 12,
            "device": "AUTO",
            "depth_convention": "auto",
            "probe_identity": True,
        },
    },
    {
        "id": "analytical_thr_0_01_noLT",
        "priority": 4,
        "runner": "exp_render_stack_analytical.py",
        "timeout_s": 1200,
        "why": "Combine the two best single knobs from the analytical OFAT sweep.",
        "config": {
            "frames": 8,
            "keyframe_every": 2,
            "draft_spp": 512,
            "ref_spp": 4096,
            "adaptive_threshold": 0.02,
            "denoiser": "oidn",
            "denoise_guides": True,
            "light_tree": False,
            "hole_fill": "rerender",
            "disocclusion_thresh": 0.01,
            "resolution": "1920x1080",
            "scene": "classroom",
            "bounces": 12,
            "device": "AUTO",
            "depth_convention": "auto",
            "probe_identity": True,
        },
    },
    {
        "id": "ultimate_no_reprojection",
        "priority": 5,
        "runner": "exp_render_ultimate.py",
        "timeout_s": 3600,
        "why": "Honest product number: denoise anchor + light-tree + VP9, no temporal reuse.",
        "config": {
            "frames": 8,
            "keyframe_every": 1,
            "draft_spp": 512,
            "ref_spp": 4096,
            "adaptive_threshold": 0.02,
            "denoiser": "oidn",
            "denoise_guides": True,
            "light_tree": True,
            "codec": "libvpx-vp9",
            "resolution": "1920x1080",
            "scene": "classroom",
            "bounces": 12,
            "device": "AUTO",
        },
    },
    {
        "id": "upscale_guided_all",
        "priority": 6,
        "runner": "exp_render_upscale_guided.py",
        "timeout_s": 2400,
        "why": "Test render-low/upscale-high with bicubic, AOV-guided, and Real-ESRGAN control.",
        "config": {
            "scene": "classroom",
            "low_res": "960x540",
            "full_res": "1920x1080",
            "method": "all",
            "spp": 4096,
            "guide_spp": 16,
            "bounces": 12,
            "device": "AUTO",
        },
    },
    {
        "id": "interp_flow_guided",
        "priority": 7,
        "runner": "exp_render_interp_learned.py",
        "timeout_s": 2400,
        "why": "Run the existing fixed interpolation spike before building auxiliary-view reprojection.",
        "config": {
            "frames": 8,
            "interp_every": 2,
            "model": "flow_guided",
            "scene": "animated",
            "spp": 256,
            "resolution": 384,
            "device": "AUTO",
        },
    },
    {
        "id": "bmw27_analytical_animation",
        "priority": 8,
        "runner": "exp_render_stack_analytical.py",
        "timeout_s": 1800,
        "why": "Check whether the temporal-reuse wall is Classroom-specific.",
        "config": {
            "frames": 8,
            "keyframe_every": 2,
            "draft_spp": 512,
            "ref_spp": 4096,
            "adaptive_threshold": 0.02,
            "denoiser": "oidn",
            "denoise_guides": True,
            "light_tree": True,
            "hole_fill": "rerender",
            "disocclusion_thresh": 0.02,
            "resolution": "1920x1080",
            "scene": "bmw27",
            "bounces": 12,
            "device": "AUTO",
            "depth_convention": "auto",
            "probe_identity": True,
        },
    },
]


def pod_command(item):
    if item["runner"].startswith("driver:"):
        return item["runner"].split(":", 1)[1]
    return (
        "cd /root/spec-lab && python3 pod/{runner} '{payload}'"
        .format(runner=item["runner"], payload=json.dumps(item["config"], separators=(",", ":")))
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--markdown", action="store_true", help="print a markdown checklist")
    args = parser.parse_args()

    if args.markdown:
        for item in QUEUE:
            print(f"- [ ] P{item['priority']} `{item['id']}` - {item['why']}")
            print(f"      runner: `{item['runner']}`")
            print(f"      timeout_s: `{item['timeout_s']}`")
            print(f"      pod_command: `{pod_command(item)}`")
        return

    for item in QUEUE:
        row = dict(item)
        row["pod_command"] = pod_command(item)
        print(json.dumps(row, sort_keys=True))


if __name__ == "__main__":
    main()
