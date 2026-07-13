#!/usr/bin/env python3
"""Bounded one-target-render Koro OIDN/confidence frontier screen.

The ``render`` command runs inside pinned Blender 4.2.  It delegates the
one-target-render OIDN extraction contract to ``screen_blender_oidn_multipass``
and extends that same compositor invocation with Denoising Depth and Debug
Sample Count.  Blender 4.2 exposes no variance pass; Debug Sample Count is only
a confidence-like diagnostic and is uniform under this screen's fixed sampling.

The ``finalize`` command runs under the repository Python.  It reconstructs
bounded 1080x1920 candidates from the same-render noisy/denoised pair, computes
descriptive confidence diagnostics from only those same-render passes, and runs
quality-v3 against an existing reference.  Same-render confidence is explicitly
non-independent and can never authorize a production verifier change.

The ``sweep`` command is a CPU-only, bounded follow-up over an already closed
report.  It measures the full blend/resize/encode/validate reconstruction path
for eight frozen residual fractions and writes a separate fail-closed report;
it never invokes Blender or broadens the confidence claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sys
import time
from typing import Any, Sequence

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import screen_blender_oidn_multipass as base


KIND = "cx_blender_one_render_confidence_screen"
SCHEMA_VERSION = 1
REPORT_NAME = "one-render-confidence-screen.json"
SWEEP_REPORT_NAME = "one-render-residual-sweep.json"
BASE_REPORT_NAME = base.REPORT_NAME
TARGET_SIZE = (1080, 1920)
MAX_CANDIDATES = 8
SWEEP_FRACTIONS = (0.30, 0.40, 0.45, 0.475, 0.50, 0.55, 0.65, 0.80)

EXTRA_PASS_TO_STAGE = {
    "Denoising Depth": "depth",
    "Debug Sample Count": "sample_count",
}
CAPTURED_STAGES = {
    "denoised",
    "noisy",
    "albedo",
    "normal",
    "depth",
    "sample_count",
}
CONFIDENCE_THRESHOLDS = {
    "same_render_rgb_agreement_minimum": 0.80,
    "same_render_luma_residual_p99_maximum": 0.60,
    "sample_count_range_maximum": 1e-5,
    "guide_finite_fraction_minimum": 1.0,
    "guide_activity_std_minimum": 1e-6,
}
CANDIDATE_SPECS = (
    {"name": "oidn_bicubic", "noisy_fraction": 0.0, "resample": "BICUBIC"},
    {"name": "oidn_lanczos", "noisy_fraction": 0.0, "resample": "LANCZOS"},
    {"name": "residual05_bicubic", "noisy_fraction": 0.05, "resample": "BICUBIC"},
    {"name": "residual10_bicubic", "noisy_fraction": 0.10, "resample": "BICUBIC"},
    {
        "name": "guide_edge10_bicubic",
        "noisy_fraction": 0.10,
        "resample": "BICUBIC",
        "guide_edge_weighted": True,
    },
)


class OneRenderError(ValueError):
    """The bounded experiment or one-render attestation failed closed."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> tuple[str, int]:
    return base._sha256_file(path)


def _round9(value: float) -> float:
    return base._round9(value)


def analyze_confidence_passes(ordered_names: Sequence[str]) -> dict[str, Any]:
    ordered = [str(value) for value in ordered_names]
    lower = [value.lower() for value in ordered]
    variance = [
        value
        for value in ordered
        if "variance" in value.lower() or "confidence" in value.lower()
    ]
    return {
        "captured_confidence_like": [
            value for value in EXTRA_PASS_TO_STAGE if value in ordered
        ],
        "debug_sample_count_available": "debug sample count" in lower,
        "denoising_depth_available": "denoising depth" in lower,
        "ordered_socket_names": ordered,
        "true_variance_or_confidence_passes": variance,
        "true_variance_pass_available": bool(variance),
        "blender_4_2_fixed_sampling_note": (
            "Debug Sample Count is uniform under fixed sampling and is not a "
            "per-pixel variance estimate."
        ),
    }


def _extended_build_stage_graph(
    original: Any,
    scene: Any,
    view_layer: Any,
    stage_dir: Path,
) -> tuple[dict[str, Any], dict[str, str]]:
    base._require_property(view_layer.cycles, "pass_debug_sample_count", True)
    result, prefixes = original(scene, view_layer, stage_dir)
    render_layers = scene.node_tree.nodes.get("CX OIDN Source")
    if render_layers is None:
        raise OneRenderError("base OIDN render-layer node disappeared")
    links = list(result["graph"]["links"])
    for pass_name, stage_name in EXTRA_PASS_TO_STAGE.items():
        socket = render_layers.outputs.get(pass_name)
        if socket is None:
            raise OneRenderError(f"Blender 4.2 pass unavailable: {pass_name}")
        prefix = f"cx-{stage_name}-"
        node = scene.node_tree.nodes.new("CompositorNodeOutputFile")
        node.name = f"CX One Render {stage_name.title()} Stage"
        node.label = f"CX {pass_name} confidence stage"
        node.base_path = str(stage_dir)
        node.format.file_format = "OPEN_EXR"
        node.format.color_mode = "RGBA"
        node.format.color_depth = "32"
        node.format.exr_codec = "ZIP"
        node.file_slots[0].path = prefix
        scene.node_tree.links.new(socket, node.inputs[0])
        prefixes[stage_name] = prefix
        links.append(
            {
                "destination": f"{node.name}.Image",
                "pass": pass_name,
                "source": f"{render_layers.name}.{pass_name}",
                "stage": stage_name,
            }
        )
    result["graph"]["links"] = links
    result["graph"]["links_sha256"] = base._canonical_sha256(links)
    return result, prefixes


def render_inside_blender(args: argparse.Namespace) -> dict[str, Any]:
    try:
        import bpy  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - Blender boundary
        raise OneRenderError("render command must run inside Blender") from exc

    original = base._build_stage_graph

    def extended(scene: Any, view_layer: Any, stage_dir: Path) -> Any:
        return _extended_build_stage_graph(
            original, scene, view_layer, stage_dir
        )

    base._build_stage_graph = extended
    try:
        report = base._run_blender(args)
    finally:
        base._build_stage_graph = original

    output_dir = args.output_dir.resolve(strict=True)
    stage_dir = output_dir / "float-stages"
    extra_paths = base._discover_stage_files(
        stage_dir,
        {stage: f"cx-{stage}-" for stage in EXTRA_PASS_TO_STAGE.values()},
    )
    source_render_id = report["render"]["source_render_id"]
    for stage_name in ("depth", "sample_count"):
        stage, image = base._float_image_attestation(
            bpy,
            extra_paths[stage_name],
            expected_width=args.width,
            expected_height=args.height,
            source_render_id=source_render_id,
        )
        report["stages"][stage_name] = stage
        bpy.data.images.remove(image)

    capabilities = analyze_confidence_passes(
        report["pass_manifest"]["ordered_socket_names"]
    )
    if set(report["stages"]) != CAPTURED_STAGES:
        raise OneRenderError("same-render captured stage set changed")
    if set(capabilities["captured_confidence_like"]) != set(EXTRA_PASS_TO_STAGE):
        raise OneRenderError("required confidence-like passes were not captured")

    report["kind"] = KIND
    report["schema_version"] = SCHEMA_VERSION
    report["pass_capabilities"] = capabilities
    selected_layer = base._select_view_layer(bpy.context.scene, args.view_layer)
    report["configuration"]["confidence_pass_policy"] = {
        "denoising_depth_from_same_render": True,
        "pass_debug_sample_count": bool(
            selected_layer.cycles.pass_debug_sample_count
        ),
        "use_adaptive_sampling": False,
        "variance_pass_available": capabilities["true_variance_pass_available"],
    }
    report["decision"].update(
        {
            "confidence_is_independent": False,
            "independent_second_seed_rendered": False,
            "same_render_confidence_only": True,
            "quality_authorized": False,
            "production_change_authorized": False,
        }
    )
    report["limitations"].extend(
        [
            "no independent second stochastic seed is rendered",
            "OIDN output, noisy input, and guides all derive from one sample range",
            "Blender 4.2 exposes no true variance pass in this configuration",
            "Debug Sample Count is uniform under fixed non-adaptive sampling",
        ]
    )
    report["finalization"] = {
        "closed": False,
        "quality_v3_run": False,
        "reference_used_for_same_render_confidence": False,
    }
    base_report = output_dir / BASE_REPORT_NAME
    if base_report.exists():
        base_report.unlink()
    base._write_json_atomic(output_dir / REPORT_NAME, report)
    return report


def _load_exr(path: Path) -> Any:
    try:
        import numpy as np
        import OpenImageIO as oiio
    except ImportError as exc:
        raise OneRenderError("finalizer requires numpy and OpenImageIO") from exc
    source = oiio.ImageInput.open(str(path))
    if source is None:
        raise OneRenderError(f"cannot open EXR: {path}")
    try:
        spec = source.spec()
        values = source.read_image(oiio.FLOAT)
    finally:
        source.close()
    array = np.asarray(values, dtype=np.float32)
    expected = int(spec.width) * int(spec.height) * int(spec.nchannels)
    if array.size != expected or int(spec.nchannels) < 1:
        raise OneRenderError(f"EXR decoded shape changed: {path}")
    result = array.reshape(int(spec.height), int(spec.width), int(spec.nchannels))
    if not np.isfinite(result).all():
        raise OneRenderError(f"EXR contains non-finite values: {path}")
    return result


def _guide_edge_map(albedo: Any, normal: Any) -> Any:
    import numpy as np

    def gradient(value: Any) -> Any:
        channels = value[..., : min(3, value.shape[2])].astype(np.float64)
        gx = np.zeros(channels.shape[:2], dtype=np.float64)
        gy = np.zeros_like(gx)
        gx[:, 1:] = np.mean(np.abs(channels[:, 1:] - channels[:, :-1]), axis=2)
        gy[1:, :] = np.mean(np.abs(channels[1:, :] - channels[:-1, :]), axis=2)
        return np.hypot(gx, gy)

    edge = gradient(albedo) + gradient(normal)
    positive = edge[edge > 0]
    if not positive.size:
        return np.zeros_like(edge)
    scale = float(np.quantile(positive, 0.90, method="linear"))
    if not math.isfinite(scale) or scale <= 0:
        return np.zeros_like(edge)
    return np.clip(edge / scale, 0.0, 1.0)


def same_render_confidence(
    denoised_rgba: Any,
    noisy_rgba: Any,
    *,
    albedo: Any,
    normal: Any,
    depth: Any,
    sample_count: Any,
) -> dict[str, Any]:
    """A descriptive same-render confidence gate; never independent evidence."""
    import numpy as np

    denoised = np.asarray(denoised_rgba, dtype=np.float64)
    noisy = np.asarray(noisy_rgba, dtype=np.float64)
    if denoised.shape != noisy.shape or denoised.ndim != 3 or denoised.shape[2] != 4:
        raise OneRenderError("same-render image buffers are incompatible")
    if denoised.max(initial=0.0) > 1.0 or noisy.max(initial=0.0) > 1.0:
        denoised = denoised / 255.0
        noisy = noisy / 255.0
    residual = np.abs(denoised[..., :3] - noisy[..., :3])
    luma_residual = np.sum(
        residual * np.array([0.2126, 0.7152, 0.0722]), axis=2
    )
    guides = (albedo, normal, depth, sample_count)
    finite_fraction = min(
        float(np.count_nonzero(np.isfinite(value)) / value.size)
        for value in guides
    )
    guide_stds = {
        "albedo": float(np.std(albedo[..., : min(3, albedo.shape[2])])),
        "normal": float(np.std(normal[..., : min(3, normal.shape[2])])),
        "depth": float(np.std(depth[..., 0])),
    }
    sample_values = np.asarray(sample_count[..., 0], dtype=np.float64)
    sample_range = float(np.max(sample_values) - np.min(sample_values))
    agreement = 1.0 - float(np.mean(residual))
    p99 = float(np.quantile(luma_residual, 0.99, method="linear"))
    checks = {
        "same_render_rgb_agreement": {
            "value": _round9(agreement),
            "minimum": CONFIDENCE_THRESHOLDS[
                "same_render_rgb_agreement_minimum"
            ],
            "pass": agreement
            >= CONFIDENCE_THRESHOLDS["same_render_rgb_agreement_minimum"],
        },
        "same_render_luma_residual_p99": {
            "value": _round9(p99),
            "maximum": CONFIDENCE_THRESHOLDS[
                "same_render_luma_residual_p99_maximum"
            ],
            "pass": p99
            <= CONFIDENCE_THRESHOLDS[
                "same_render_luma_residual_p99_maximum"
            ],
        },
        "sample_count_range": {
            "value": _round9(sample_range),
            "maximum": CONFIDENCE_THRESHOLDS["sample_count_range_maximum"],
            "pass": sample_range
            <= CONFIDENCE_THRESHOLDS["sample_count_range_maximum"],
        },
        "guide_finite_fraction": {
            "value": _round9(finite_fraction),
            "minimum": CONFIDENCE_THRESHOLDS["guide_finite_fraction_minimum"],
            "pass": finite_fraction
            >= CONFIDENCE_THRESHOLDS["guide_finite_fraction_minimum"],
        },
        "guide_activity": {
            "values": {name: _round9(value) for name, value in guide_stds.items()},
            "minimum": CONFIDENCE_THRESHOLDS["guide_activity_std_minimum"],
            "pass": guide_stds["albedo"]
            >= CONFIDENCE_THRESHOLDS["guide_activity_std_minimum"]
            and guide_stds["normal"]
            >= CONFIDENCE_THRESHOLDS["guide_activity_std_minimum"],
        },
    }
    return {
        "checks": checks,
        "confidence_gate_pass": all(check["pass"] for check in checks.values()),
        "descriptive_only": True,
        "independent_verification": False,
        "quality_authorized": False,
        "same_render_sample_reuse": True,
        "thresholds": dict(CONFIDENCE_THRESHOLDS),
    }


def _candidate_lowres(
    denoised: Any,
    noisy: Any,
    spec: dict[str, Any],
    guide_edge: Any,
) -> Any:
    import numpy as np

    fraction = float(spec["noisy_fraction"])
    if spec.get("guide_edge_weighted"):
        weight = (fraction * guide_edge)[..., None]
    else:
        weight = fraction
    rgb = np.rint(
        np.clip(
            denoised[..., :3].astype(np.float64) * (1.0 - weight)
            + noisy[..., :3].astype(np.float64) * weight,
            0.0,
            255.0,
        )
    ).astype(np.uint8)
    alpha = denoised[..., 3:4].astype(np.uint8)
    return np.concatenate((rgb, alpha), axis=2)


def _write_upscaled_candidate(
    lowres: Any,
    output: Path,
    *,
    resample_name: str,
) -> dict[str, Any]:
    import numpy as np
    from PIL import Image

    if output.exists():
        raise OneRenderError(f"candidate output already exists: {output}")
    resampling = getattr(Image.Resampling, resample_name)
    started = time.perf_counter()
    rgb = Image.fromarray(np.asarray(lowres[..., :3], dtype=np.uint8), "RGB").resize(
        TARGET_SIZE, resampling
    )
    alpha = Image.fromarray(np.asarray(lowres[..., 3], dtype=np.uint8), "L").resize(
        TARGET_SIZE, Image.Resampling.BICUBIC
    )
    image = Image.merge("RGBA", (*rgb.split(), alpha))
    image.save(output, format="PNG", optimize=False, compress_level=0)
    reconstruction_wall_s = time.perf_counter() - started
    with Image.open(output) as check:
        if check.format != "PNG" or check.mode != "RGBA" or check.size != TARGET_SIZE:
            raise OneRenderError("candidate PNG identity changed")
        check.load()
    digest, byte_count = _sha256_file(output)
    return {
        "bytes": byte_count,
        "path": output.name,
        "reconstruction_wall_s": _round9(reconstruction_wall_s),
        "resample_rgb": resample_name,
        "resample_alpha": "BICUBIC",
        "sha256": digest,
    }


def _stage_path(root: Path, stage: dict[str, Any]) -> Path:
    path = root / "float-stages" / stage["exr_path"]
    digest, byte_count = _sha256_file(path)
    if digest != stage["exr_sha256"] or byte_count != stage["exr_bytes"]:
        raise OneRenderError(f"staged EXR identity mismatch: {path}")
    return path


def finalize_report(
    render_report_path: Path,
    reference: Path,
    *,
    baseline_s: float,
) -> dict[str, Any]:
    import numpy as np
    from PIL import Image
    import cx_render_quality_v3 as quality

    started = time.perf_counter()
    root = render_report_path.parent.resolve(strict=True)
    report = json.loads(render_report_path.read_text())
    validate_render_report(report)
    if report["finalization"]["closed"]:
        raise OneRenderError("report is already closed")
    settings = report["configuration"]["exact_target_settings"]
    if (settings["width"], settings["height"]) not in {(810, 1440), (540, 960)}:
        raise OneRenderError("finalizer accepts only frozen Koro screen sizes")
    if settings["frame"] != 9 or settings["samples"] not in {1, 2, 4}:
        raise OneRenderError("finalizer accepts only frame 9 at 1/2/4 SPP")
    if not math.isfinite(baseline_s) or baseline_s <= 0:
        raise OneRenderError("baseline seconds must be finite and positive")

    reference_sha, reference_bytes = _sha256_file(reference)
    with Image.open(reference) as ref:
        if ref.format != "PNG" or ref.size != TARGET_SIZE:
            raise OneRenderError("existing reference identity mismatch")
        ref.load()

    load_started = time.perf_counter()
    decoded_images: dict[str, Any] = {}
    for name in ("denoised", "noisy"):
        output = report["outputs"][name]
        path = root / output["path"]
        digest, byte_count = _sha256_file(path)
        if digest != output["sha256"] or byte_count != output["bytes"]:
            raise OneRenderError(f"same-render PNG identity mismatch: {name}")
        with Image.open(path) as image:
            image.load()
            decoded_images[name] = np.asarray(image.convert("RGBA"), dtype=np.uint8)
    guides = {
        name: _load_exr(_stage_path(root, report["stages"][name]))
        for name in ("albedo", "normal", "depth", "sample_count")
    }
    common_load_wall_s = time.perf_counter() - load_started
    confidence_started = time.perf_counter()
    confidence = same_render_confidence(
        decoded_images["denoised"],
        decoded_images["noisy"],
        **guides,
    )
    guide_edge = _guide_edge_map(guides["albedo"], guides["normal"])
    confidence_wall_s = time.perf_counter() - confidence_started

    candidates: list[dict[str, Any]] = []
    for spec in CANDIDATE_SPECS[:MAX_CANDIDATES]:
        reconstruction_started = time.perf_counter()
        lowres = _candidate_lowres(
            decoded_images["denoised"],
            decoded_images["noisy"],
            spec,
            guide_edge,
        )
        artifact = _write_upscaled_candidate(
            lowres,
            root / f"candidate-{spec['name']}.png",
            resample_name=spec["resample"],
        )
        artifact["resize_encode_validate_wall_s"] = artifact[
            "reconstruction_wall_s"
        ]
        artifact["reconstruction_wall_s"] = _round9(
            time.perf_counter() - reconstruction_started
        )
        audit_started = time.perf_counter()
        audit = quality.evaluate_pngs(
            root / artifact["path"], reference, target_size=TARGET_SIZE
        )
        audit_wall_s = time.perf_counter() - audit_started
        candidates.append(
            {
                **artifact,
                "name": spec["name"],
                "noisy_fraction": spec["noisy_fraction"],
                "guide_edge_weighted": bool(spec.get("guide_edge_weighted", False)),
                "quality_v3": audit,
                "quality_v3_pass": audit["pass"],
                "quality_v3_wall_s": _round9(audit_wall_s),
            }
        )

    extraction_s = sum(
        float(stage["extraction_wall_s"]) for stage in report["stages"].values()
    )
    base_encoding_s = sum(
        float(output["encoding_wall_s"]) for output in report["outputs"].values()
    )
    diagnostic_s = float(report["reference_free_diagnostic"]["wall_s"])
    common_s = (
        float(report["render"]["target_render_and_exr_staging_wall_s"])
        + extraction_s
        + base_encoding_s
        + common_load_wall_s
        + confidence_wall_s
    )
    for candidate in candidates:
        full_wall = common_s + float(candidate["reconstruction_wall_s"])
        candidate["full_one_render_candidate_wall_s"] = _round9(full_wall)
        candidate["speedup_vs_existing_4096_reference_x"] = _round9(
            baseline_s / full_wall
        )
        candidate["quality_audit_charged_to_candidate_wall"] = False

    passing = [candidate for candidate in candidates if candidate["quality_v3_pass"]]
    best = min(
        passing,
        key=lambda candidate: candidate["full_one_render_candidate_wall_s"],
        default=None,
    )
    report["same_render_confidence"] = confidence
    report["candidates"] = candidates
    report["timing"] = {
        "base_output_encoding_wall_s": _round9(base_encoding_s),
        "common_candidate_wall_s_before_reconstruction": _round9(common_s),
        "confidence_wall_s": _round9(confidence_wall_s),
        "extraction_wall_s": _round9(extraction_s),
        "finalizer_load_wall_s": _round9(common_load_wall_s),
        "quality_audit_excluded_from_candidate_wall": True,
        "legacy_descriptive_diagnostic_measurement_only_wall_s": _round9(
            diagnostic_s
        ),
        "legacy_descriptive_diagnostic_excluded_from_candidate_wall": True,
        "warmup_excluded_from_candidate_wall": True,
    }
    report["reference_audit"] = {
        "baseline_s": _round9(baseline_s),
        "bytes": reference_bytes,
        "path": str(reference.resolve(strict=True)),
        "sha256": reference_sha,
        "target_size": list(TARGET_SIZE),
    }
    report["frontier_conclusion"] = {
        "best_quality_passing_candidate": best["name"] if best else None,
        "best_quality_passing_speedup_x": (
            best["speedup_vs_existing_4096_reference_x"] if best else None
        ),
        "best_quality_passing_wall_s": (
            best["full_one_render_candidate_wall_s"] if best else None
        ),
        "independent_verification": False,
        "quality_v3_pass_count": len(passing),
        "reaches_1000x": bool(
            best and best["speedup_vs_existing_4096_reference_x"] >= 1000.0
        ),
        "same_render_confidence_can_replace_two_seed_verification": False,
    }
    report["finalization"] = {
        "closed": True,
        "finalizer_host": {
            "machine": platform.machine(),
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "quality_v3_run": True,
        "reference_used_for_same_render_confidence": False,
        "wall_s": _round9(time.perf_counter() - started),
    }
    validate_closed_report(report)
    base._write_json_atomic(render_report_path, report)
    return report


def sweep_closed_report(
    render_report_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Run the frozen CPU-only residual sweep over one closed source render."""
    import numpy as np
    from PIL import Image
    import cx_render_quality_v3 as quality

    started = time.perf_counter()
    source_root = render_report_path.parent.resolve(strict=True)
    source_bytes = render_report_path.read_bytes()
    source = json.loads(source_bytes)
    validate_closed_report(source)
    settings = source["configuration"]["exact_target_settings"]
    if (settings["width"], settings["height"], settings["frame"], settings["samples"]) != (
        810,
        1440,
        9,
        4,
    ):
        raise OneRenderError("residual sweep accepts only the frozen 810x1440 s4 arm")
    if len(SWEEP_FRACTIONS) != MAX_CANDIDATES:
        raise OneRenderError("residual sweep candidate bound changed")

    root = base._prepare_new_output_dir(output_dir)
    reference = Path(source["reference_audit"]["path"])
    reference_sha, reference_bytes = _sha256_file(reference)
    if (
        reference_sha != source["reference_audit"]["sha256"]
        or reference_bytes != source["reference_audit"]["bytes"]
    ):
        raise OneRenderError("residual sweep reference identity mismatch")

    load_started = time.perf_counter()
    decoded_images: dict[str, Any] = {}
    for name in ("denoised", "noisy"):
        output = source["outputs"][name]
        path = source_root / output["path"]
        digest, byte_count = _sha256_file(path)
        if digest != output["sha256"] or byte_count != output["bytes"]:
            raise OneRenderError(f"same-render PNG identity mismatch: {name}")
        with Image.open(path) as image:
            image.load()
            decoded_images[name] = np.asarray(image.convert("RGBA"), dtype=np.uint8)
    sweep_load_wall_s = time.perf_counter() - load_started

    common_s = float(source["timing"]["common_candidate_wall_s_before_reconstruction"])
    baseline_s = float(source["reference_audit"]["baseline_s"])
    candidates: list[dict[str, Any]] = []
    for fraction in SWEEP_FRACTIONS:
        name = f"residual{int(round(fraction * 1000)):03d}_bicubic"
        reconstruction_started = time.perf_counter()
        lowres = _candidate_lowres(
            decoded_images["denoised"],
            decoded_images["noisy"],
            {"noisy_fraction": fraction},
            np.zeros(decoded_images["denoised"].shape[:2], dtype=np.float64),
        )
        artifact = _write_upscaled_candidate(
            lowres,
            root / f"candidate-{name}.png",
            resample_name="BICUBIC",
        )
        artifact["resize_encode_validate_wall_s"] = artifact[
            "reconstruction_wall_s"
        ]
        artifact["reconstruction_wall_s"] = _round9(
            time.perf_counter() - reconstruction_started
        )
        audit_started = time.perf_counter()
        audit = quality.evaluate_pngs(
            root / artifact["path"], reference, target_size=TARGET_SIZE
        )
        audit_wall_s = time.perf_counter() - audit_started
        full_wall = common_s + float(artifact["reconstruction_wall_s"])
        candidates.append(
            {
                **artifact,
                "full_one_render_candidate_wall_s": _round9(full_wall),
                "name": name,
                "noisy_fraction": fraction,
                "quality_audit_charged_to_candidate_wall": False,
                "quality_v3": audit,
                "quality_v3_pass": audit["pass"],
                "quality_v3_wall_s": _round9(audit_wall_s),
                "speedup_vs_existing_4096_reference_x": _round9(
                    baseline_s / full_wall
                ),
            }
        )

    passing = [candidate for candidate in candidates if candidate["quality_v3_pass"]]
    fastest = min(
        passing,
        key=lambda candidate: candidate["full_one_render_candidate_wall_s"],
        default=None,
    )
    minimum_fraction = min(
        passing,
        key=lambda candidate: candidate["noisy_fraction"],
        default=None,
    )
    policy = {
        "alpha_resample": "BICUBIC",
        "fractions": list(SWEEP_FRACTIONS),
        "independent_verification": False,
        "maximum_candidates": MAX_CANDIDATES,
        "rgb_resample": "BICUBIC",
        "same_render_pair_only": True,
    }
    report = {
        "candidates": candidates,
        "closed": True,
        "conclusion": {
            "fastest_quality_passing_candidate": fastest["name"] if fastest else None,
            "fastest_quality_passing_speedup_x": (
                fastest["speedup_vs_existing_4096_reference_x"] if fastest else None
            ),
            "fastest_quality_passing_wall_s": (
                fastest["full_one_render_candidate_wall_s"] if fastest else None
            ),
            "minimum_fraction_quality_passing_candidate": (
                minimum_fraction["name"] if minimum_fraction else None
            ),
            "minimum_fraction_quality_passing_value": (
                minimum_fraction["noisy_fraction"] if minimum_fraction else None
            ),
            "pass_count": len(passing),
            "reaches_1000x": bool(
                fastest
                and fastest["speedup_vs_existing_4096_reference_x"] >= 1000.0
            ),
            "same_render_confidence_can_authorize_quality": False,
        },
        "kind": "cx_one_render_cpu_residual_sweep",
        "policy": policy,
        "policy_sha256": base._canonical_sha256(policy),
        "reference_audit": source["reference_audit"],
        "schema_version": 1,
        "source": {
            "path": str(render_report_path.resolve(strict=True)),
            "report_sha256": _sha256_bytes(source_bytes),
            "source_render_id": source["render"]["source_render_id"],
            "target_render_invocations": source["render"]["target_render_invocations"],
        },
        "timing": {
            "common_candidate_wall_s_before_reconstruction": _round9(common_s),
            "quality_audit_charged_to_candidate_wall": False,
            "sweep_artifact_load_excluded_from_candidate_wall": True,
            "sweep_artifact_load_wall_s": _round9(sweep_load_wall_s),
            "sweep_total_wall_s": _round9(time.perf_counter() - started),
        },
    }
    validate_sweep_report(report)
    base._write_json_atomic(root / SWEEP_REPORT_NAME, report)
    return report


def validate_sweep_report(report: dict[str, Any]) -> None:
    if report.get("kind") != "cx_one_render_cpu_residual_sweep":
        raise OneRenderError("residual sweep identity mismatch")
    if report.get("closed") is not True or report.get("schema_version") != 1:
        raise OneRenderError("residual sweep is not closed")
    source = report.get("source", {})
    if source.get("target_render_invocations") != 1:
        raise OneRenderError("residual sweep escaped one target render")
    policy = report.get("policy", {})
    if (
        policy.get("independent_verification") is not False
        or policy.get("same_render_pair_only") is not True
        or policy.get("fractions") != list(SWEEP_FRACTIONS)
        or report.get("policy_sha256") != base._canonical_sha256(policy)
    ):
        raise OneRenderError("residual sweep policy changed")
    candidates = report.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != MAX_CANDIDATES:
        raise OneRenderError("residual sweep candidate bound changed")
    if any(
        candidate.get("quality_audit_charged_to_candidate_wall") is not False
        for candidate in candidates
    ):
        raise OneRenderError("residual sweep timing accounting changed")
    passing = sum(bool(candidate.get("quality_v3_pass")) for candidate in candidates)
    conclusion = report.get("conclusion", {})
    if (
        conclusion.get("pass_count") != passing
        or conclusion.get("same_render_confidence_can_authorize_quality") is not False
    ):
        raise OneRenderError("residual sweep conclusion changed")


def validate_render_report(report: dict[str, Any]) -> None:
    if report.get("kind") != KIND or report.get("schema_version") != SCHEMA_VERSION:
        raise OneRenderError("one-render report identity mismatch")
    render = report.get("render", {})
    if render.get("target_render_invocations") != 1:
        raise OneRenderError("screen must contain exactly one target render")
    if set(report.get("stages", {})) != CAPTURED_STAGES:
        raise OneRenderError("captured same-render stage set mismatch")
    source_id = render.get("source_render_id")
    if not base._is_sha256(source_id):
        raise OneRenderError("source render binding is malformed")
    if any(stage.get("source_render_id") != source_id for stage in report["stages"].values()):
        raise OneRenderError("stage escaped the one source render")
    decision = report.get("decision", {})
    if (
        decision.get("confidence_is_independent") is not False
        or decision.get("independent_second_seed_rendered") is not False
        or decision.get("quality_authorized") is not False
        or decision.get("production_change_authorized") is not False
    ):
        raise OneRenderError("same-render confidence claim broadened")
    capabilities = report.get("pass_capabilities", {})
    expected = analyze_confidence_passes(
        report.get("pass_manifest", {}).get("ordered_socket_names", [])
    )
    if capabilities != expected:
        raise OneRenderError("confidence pass capability report changed")


def validate_closed_report(report: dict[str, Any]) -> None:
    validate_render_report(report)
    if report.get("finalization", {}).get("closed") is not True:
        raise OneRenderError("experimental report is not closed")
    confidence = report.get("same_render_confidence", {})
    if (
        confidence.get("independent_verification") is not False
        or confidence.get("quality_authorized") is not False
        or confidence.get("descriptive_only") is not True
    ):
        raise OneRenderError("same-render confidence semantics changed")
    candidates = report.get("candidates")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= MAX_CANDIDATES:
        raise OneRenderError("candidate set is unbounded or empty")
    if any(
        candidate.get("quality_audit_charged_to_candidate_wall") is not False
        for candidate in candidates
    ):
        raise OneRenderError("candidate timing accounting changed")
    conclusion = report.get("frontier_conclusion", {})
    if conclusion.get("same_render_confidence_can_replace_two_seed_verification") is not False:
        raise OneRenderError("same-render confidence replaced independent verification")


def _parse_outer(argv: Sequence[str]) -> tuple[str, Any]:
    if not argv:
        raise OneRenderError("expected render, probe, finalize, or sweep command")
    command, rest = argv[0], list(argv[1:])
    if command == "probe":
        return command, base._parse_args(["--probe-only"])
    if command == "render":
        args = base._parse_args(rest)
        if (args.width, args.height) not in {(810, 1440), (540, 960)}:
            raise OneRenderError("render size must be 810x1440 or 540x960")
        if args.frame != 9 or args.samples not in {1, 2, 4}:
            raise OneRenderError("render must be frame 9 at 1/2/4 SPP")
        return command, args
    if command == "finalize":
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--render-report", type=Path, required=True)
        parser.add_argument("--reference", type=Path, required=True)
        parser.add_argument("--baseline-s", type=float, required=True)
        args = parser.parse_args(rest)
        if not args.render_report.is_absolute() or not args.reference.is_absolute():
            parser.error("render-report and reference must be absolute")
        return command, args
    if command == "sweep":
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--render-report", type=Path, required=True)
        parser.add_argument("--output-dir", type=Path, required=True)
        args = parser.parse_args(rest)
        if not args.render_report.is_absolute() or not args.output_dir.is_absolute():
            parser.error("render-report and output-dir must be absolute")
        return command, args
    raise OneRenderError(f"unknown command: {command}")


def _cli_argv(argv: Sequence[str]) -> list[str]:
    values = list(argv)
    if "--" in values:
        return values[values.index("--") + 1 :]
    return values[1:]


def main(argv: Sequence[str] | None = None) -> int:
    raw = sys.argv if argv is None else argv
    try:
        command, args = _parse_outer(_cli_argv(raw))
        if command == "probe":
            result = base._probe_only(args)
            result["kind"] = KIND
            result["confidence_pass_analysis"] = analyze_confidence_passes(
                result["pass_manifest"]["ordered_socket_names"]
            )
        elif command == "render":
            report = render_inside_blender(args)
            result = {
                "kind": KIND,
                "ok": True,
                "report": REPORT_NAME,
                "target_render_invocations": report["render"][
                    "target_render_invocations"
                ],
            }
        elif command == "finalize":
            report = finalize_report(
                args.render_report,
                args.reference,
                baseline_s=args.baseline_s,
            )
            result = {
                "frontier_conclusion": report["frontier_conclusion"],
                "kind": KIND,
                "ok": True,
                "report": REPORT_NAME,
            }
        else:
            report = sweep_closed_report(args.render_report, args.output_dir)
            result = {
                "conclusion": report["conclusion"],
                "kind": report["kind"],
                "ok": True,
                "report": SWEEP_REPORT_NAME,
            }
        print(json.dumps(result, sort_keys=True, allow_nan=False), flush=True)
        return 0
    except BaseException as exc:
        print(
            json.dumps(
                {
                    "error": f"{type(exc).__name__}: {exc}"[:1000],
                    "kind": KIND,
                    "ok": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
