#!/usr/bin/env python3
"""Experimental one-target-render Cycles/OIDN multipass screen.

Run the measurement inside the operator-pinned Blender 4.2 executable::

    Blender -b --factory-startup --disable-autoexec --python \
      scripts/spec-lab/screen_blender_oidn_multipass.py -- \
      --scene /absolute/main.blend --output-dir /absolute/new-output-dir \
      --width 512 --height 288 --frame 1 --samples 4 --device METAL

The harness warms Cycles with a 1x1 render, then invokes the target
scene/frame/resolution/sample configuration exactly once.  Native Cycles OIDN
is pinned to RGB+albedo+normal, ACCURATE, HIGH, CPU.  One compositor Render
Layers node routes ``Image`` (the native denoised Combined result), ``Noisy
Image``, ``Denoising Albedo``, and ``Denoising Normal`` to individual float EXR
staging files during that single target invocation.  The EXRs are then loaded,
hashed, and the two image passes are encoded as strict 8-bit RGBA PNGs without
rerendering.

Blender 4.2 does not expose Render Result passes as ``Image.layers`` in Python.
The compositor staging boundary is therefore intentional and is fully charged
to ``target_render_and_exr_staging_wall_s``.  Post-render EXR extraction and
each final PNG encoding are timed separately.  This screen is experimental: its
reference-free noisy/denoised diagnostic is descriptive only and cannot approve
quality, a verifier change, or a production backend change.

Use ``--probe-only`` under ``--factory-startup`` for a no-render/no-write RNA
and compositor-pass enumeration probe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import struct
import sys
import time
from typing import Any, Iterable
import zlib


KIND = "cx_blender_oidn_multipass_screen"
SCHEMA_VERSION = 1
REPORT_NAME = "oidn-multipass-screen.json"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_DIMENSION = 4096
MAX_PIXELS = 16_777_216
MAX_SAMPLES = 4096
MAX_SAMPLE_OFFSET = (1 << 31) - 1

PAIR_PASSES = ("Image", "Noisy Image")
GUIDE_PASSES = ("Denoising Albedo", "Denoising Normal")
REQUIRED_PASSES = PAIR_PASSES + GUIDE_PASSES
STAGE_BY_PASS = {
    "Image": "denoised",
    "Noisy Image": "noisy",
    "Denoising Albedo": "albedo",
    "Denoising Normal": "normal",
}
OIDN_POLICY = {
    "denoiser": "OPENIMAGEDENOISE",
    "denoising_input_passes": "RGB_ALBEDO_NORMAL",
    "denoising_prefilter": "ACCURATE",
    "denoising_quality": "HIGH",
    "denoising_use_gpu": False,
    "scene_use_denoising": True,
    "view_layer_denoising_store_passes": True,
    "view_layer_use_denoising": True,
}


class ScreenError(ValueError):
    """Malformed input, unavailable Blender capability, or invalid artifact."""


def _round9(value: float) -> float:
    if not math.isfinite(value):
        raise ScreenError("non-finite timing or diagnostic")
    return float(f"{value:.9f}")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            byte_count += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), byte_count


def _paeth(left: int, up: int, upper_left: int) -> int:
    estimate = left + up - upper_left
    left_distance = abs(estimate - left)
    up_distance = abs(estimate - up)
    upper_left_distance = abs(estimate - upper_left)
    if left_distance <= up_distance and left_distance <= upper_left_distance:
        return left
    if up_distance <= upper_left_distance:
        return up
    return upper_left


def decode_strict_rgba_png(
    path: Path, *, expected_width: int, expected_height: int
) -> tuple[bytes, dict[str, Any]]:
    """Decode and validate the bounded PNG subset required by the screen."""
    data = path.read_bytes()
    if not data.startswith(PNG_SIGNATURE):
        raise ScreenError(f"{path}: output is not a PNG")
    cursor = len(PNG_SIGNATURE)
    header: tuple[int, int, int, int, int, int, int] | None = None
    compressed = bytearray()
    chunk_types: list[str] = []
    saw_end = False
    while cursor < len(data):
        if cursor + 12 > len(data):
            raise ScreenError(f"{path}: truncated PNG chunk")
        length = struct.unpack(">I", data[cursor : cursor + 4])[0]
        kind = data[cursor + 4 : cursor + 8]
        payload_start = cursor + 8
        payload_end = payload_start + length
        crc_end = payload_end + 4
        if crc_end > len(data):
            raise ScreenError(f"{path}: truncated PNG payload")
        payload = data[payload_start:payload_end]
        expected_crc = struct.unpack(">I", data[payload_end:crc_end])[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != expected_crc:
            raise ScreenError(f"{path}: PNG CRC mismatch")
        try:
            chunk_types.append(kind.decode("ascii"))
        except UnicodeDecodeError as exc:
            raise ScreenError(f"{path}: non-ASCII PNG chunk") from exc
        if kind == b"IHDR":
            if header is not None or length != 13:
                raise ScreenError(f"{path}: invalid PNG header")
            header = struct.unpack(">IIBBBBB", payload)
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            if length != 0 or crc_end != len(data):
                raise ScreenError(f"{path}: invalid PNG end")
            saw_end = True
            break
        cursor = crc_end
    if header is None or not saw_end or not compressed:
        raise ScreenError(f"{path}: incomplete PNG")
    width, height, depth, color_type, compression, filtering, interlace = header
    if (width, height) != (expected_width, expected_height):
        raise ScreenError(f"{path}: PNG dimensions changed")
    if (
        depth != 8
        or color_type != 6
        or compression != 0
        or filtering != 0
        or interlace != 0
    ):
        raise ScreenError(f"{path}: PNG is not strict 8-bit non-interlaced RGBA")
    stride = width * 4
    expected_size = height * (stride + 1)
    try:
        raw = zlib.decompress(bytes(compressed))
    except zlib.error as exc:
        raise ScreenError(f"{path}: PNG deflate failed: {exc}") from exc
    if len(raw) != expected_size:
        raise ScreenError(f"{path}: decoded PNG byte count changed")

    rgba = bytearray(width * height * 4)
    previous = bytearray(stride)
    source_cursor = 0
    destination_cursor = 0
    for _row in range(height):
        filter_type = raw[source_cursor]
        source_cursor += 1
        encoded = raw[source_cursor : source_cursor + stride]
        source_cursor += stride
        decoded = bytearray(stride)
        for index, encoded_value in enumerate(encoded):
            left = decoded[index - 4] if index >= 4 else 0
            up = previous[index]
            upper_left = previous[index - 4] if index >= 4 else 0
            if filter_type == 0:
                value = encoded_value
            elif filter_type == 1:
                value = encoded_value + left
            elif filter_type == 2:
                value = encoded_value + up
            elif filter_type == 3:
                value = encoded_value + ((left + up) >> 1)
            elif filter_type == 4:
                value = encoded_value + _paeth(left, up, upper_left)
            else:
                raise ScreenError(f"{path}: unsupported PNG row filter")
            decoded[index] = value & 0xFF
        rgba[destination_cursor : destination_cursor + stride] = decoded
        destination_cursor += stride
        previous = decoded
    return bytes(rgba), {
        "bit_depth": 8,
        "channels": 4,
        "chunk_types": chunk_types,
        "color_type": "RGBA",
        "height": height,
        "interlaced": False,
        "strict_8bit_rgba": True,
        "width": width,
    }


def reference_free_diagnostic(denoised: bytes, noisy: bytes) -> dict[str, Any]:
    """Describe a pair without treating either member as a quality reference."""
    if not denoised or len(denoised) != len(noisy) or len(denoised) % 4:
        raise ScreenError("decoded RGBA buffers are incompatible")
    pixel_count = len(denoised) // 4
    rgb_difference = 0
    alpha_difference = 0
    changed_pixels = 0
    maximum = 0
    denoised_luma: list[float] = []
    noisy_luma: list[float] = []
    luma_difference = 0.0
    for index in range(0, len(denoised), 4):
        channel_differences = [
            abs(denoised[index + channel] - noisy[index + channel])
            for channel in range(3)
        ]
        pixel_maximum = max(channel_differences)
        rgb_difference += sum(channel_differences)
        alpha_difference += abs(denoised[index + 3] - noisy[index + 3])
        changed_pixels += int(pixel_maximum != 0)
        maximum = max(maximum, pixel_maximum)
        dy = (
            0.2126 * denoised[index]
            + 0.7152 * denoised[index + 1]
            + 0.0722 * denoised[index + 2]
        ) / 255.0
        ny = (
            0.2126 * noisy[index]
            + 0.7152 * noisy[index + 1]
            + 0.0722 * noisy[index + 2]
        ) / 255.0
        denoised_luma.append(dy)
        noisy_luma.append(ny)
        luma_difference += abs(dy - ny)

    def population_std(values: list[float]) -> float:
        mean = sum(values) / len(values)
        return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))

    return {
        "alpha_agreement": _round9(
            1.0 - alpha_difference / (pixel_count * 255.0)
        ),
        "changed_pixel_fraction": _round9(changed_pixels / pixel_count),
        "changed_pixels": changed_pixels,
        "denoised_luma_population_std": _round9(population_std(denoised_luma)),
        "descriptive_only": True,
        "mean_abs_luma_change": _round9(luma_difference / pixel_count),
        "max_abs_rgb_channel_change": maximum,
        "noisy_luma_population_std": _round9(population_std(noisy_luma)),
        "pixel_identical": denoised == noisy,
        "quality_authorized": False,
        "rgb_mae_agreement": _round9(
            1.0 - rgb_difference / (pixel_count * 3.0 * 255.0)
        ),
    }


def analyze_pass_manifest(ordered_passes: Iterable[str]) -> dict[str, Any]:
    ordered = [str(value) for value in ordered_passes]
    availability = {name: name in ordered for name in REQUIRED_PASSES}
    pair_available = all(availability[name] for name in PAIR_PASSES)
    guides_available = all(availability[name] for name in GUIDE_PASSES)
    return {
        "availability": availability,
        "guided_pair_extractable": pair_available and guides_available,
        "missing_required": [name for name in REQUIRED_PASSES if not availability[name]],
        "ordered_socket_names": ordered,
        "ordered_socket_names_sha256": _canonical_sha256(ordered),
        "pair_extractable": pair_available,
    }


def source_render_id(
    *,
    graph_sha256: str,
    pass_manifest_sha256: str,
    scene_sha256: str,
    settings_sha256: str,
) -> str:
    """Bind the one post-warmup target invocation to its frozen inputs."""
    values = (
        graph_sha256,
        pass_manifest_sha256,
        scene_sha256,
        settings_sha256,
    )
    if not all(_is_sha256(value) for value in values):
        raise ScreenError("source-render input digest is malformed")
    return _canonical_sha256(
        {
            "graph_sha256": graph_sha256,
            "invocation_ordinal_after_warmup": 1,
            "pass_manifest_sha256": pass_manifest_sha256,
            "scene_sha256": scene_sha256,
            "settings_sha256": settings_sha256,
        }
    )


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def validate_report(report: dict[str, Any]) -> None:
    """Fail closed on the report's same-render and no-quality-claim contract."""
    if report.get("kind") != KIND or report.get("schema_version") != SCHEMA_VERSION:
        raise ScreenError("report identity mismatch")
    if report.get("experimental_only") is not True:
        raise ScreenError("screen must remain experimental")
    decision = report.get("decision")
    if not isinstance(decision, dict):
        raise ScreenError("report decision is missing")
    if decision.get("quality_authorized") is not False:
        raise ScreenError("reference-free diagnostic cannot authorize quality")
    if decision.get("production_change_authorized") is not False:
        raise ScreenError("screen cannot authorize a production change")
    render = report.get("render")
    if not isinstance(render, dict):
        raise ScreenError("render attestation is missing")
    if render.get("warmup_render_invocations") != 1:
        raise ScreenError("expected exactly one warmup invocation")
    if render.get("target_render_invocations") != 1:
        raise ScreenError("expected exactly one target render invocation")
    source_render_id_value = render.get("source_render_id")
    if not _is_sha256(source_render_id_value):
        raise ScreenError("source render id is malformed")
    for timing_name in (
        "warmup_wall_s",
        "target_render_and_exr_staging_wall_s",
    ):
        timing = render.get(timing_name)
        if not isinstance(timing, (int, float)) or not math.isfinite(timing) or timing < 0:
            raise ScreenError(f"{timing_name} is invalid")

    configuration = report.get("configuration")
    if not isinstance(configuration, dict):
        raise ScreenError("configuration is missing")
    settings = configuration.get("exact_target_settings")
    if not isinstance(settings, dict):
        raise ScreenError("exact target settings are missing")
    if configuration.get("exact_target_settings_sha256") != _canonical_sha256(settings):
        raise ScreenError("exact target settings digest mismatch")
    if settings.get("oidn_policy") != OIDN_POLICY:
        raise ScreenError("OIDN policy changed")
    graph = configuration.get("compositor_graph")
    if not isinstance(graph, dict) or not _is_sha256(graph.get("links_sha256")):
        raise ScreenError("compositor graph attestation is missing")

    pass_manifest = report.get("pass_manifest")
    if not isinstance(pass_manifest, dict):
        raise ScreenError("pass manifest is missing")
    expected_manifest = analyze_pass_manifest(pass_manifest.get("ordered_socket_names", []))
    if pass_manifest != expected_manifest:
        raise ScreenError("pass manifest is not canonical")
    if not pass_manifest["guided_pair_extractable"]:
        raise ScreenError("raw, denoised, albedo, and normal passes are required")
    scene = report.get("scene")
    if not isinstance(scene, dict) or not _is_sha256(scene.get("sha256")):
        raise ScreenError("scene attestation is missing")
    expected_source_render_id = source_render_id(
        graph_sha256=graph["links_sha256"],
        pass_manifest_sha256=pass_manifest["ordered_socket_names_sha256"],
        scene_sha256=scene["sha256"],
        settings_sha256=configuration["exact_target_settings_sha256"],
    )
    if source_render_id_value != expected_source_render_id:
        raise ScreenError("source render id does not bind the frozen render inputs")

    stages = report.get("stages")
    if not isinstance(stages, dict) or set(stages) != set(STAGE_BY_PASS.values()):
        raise ScreenError("stage set mismatch")
    for name, stage in stages.items():
        if (
            not isinstance(stage, dict)
            or stage.get("source_render_id") != source_render_id_value
        ):
            raise ScreenError(f"{name} is not bound to the one target render")
        if stage.get("nonfinite_float_count") != 0:
            raise ScreenError(f"{name} contains non-finite float pixels")
        if not _is_sha256(stage.get("exr_sha256")) or not _is_sha256(
            stage.get("float_rgba_sha256")
        ):
            raise ScreenError(f"{name} stage hashes are malformed")
        timing = stage.get("extraction_wall_s")
        if not isinstance(timing, (int, float)) or not math.isfinite(timing) or timing < 0:
            raise ScreenError(f"{name} extraction timing is invalid")

    outputs = report.get("outputs")
    if not isinstance(outputs, dict) or set(outputs) != {"denoised", "noisy"}:
        raise ScreenError("PNG output set mismatch")
    for name, output in outputs.items():
        if output.get("source_render_id") != source_render_id_value:
            raise ScreenError(f"{name} PNG is not bound to the one target render")
        if output.get("strict_png", {}).get("strict_8bit_rgba") is not True:
            raise ScreenError(f"{name} PNG contract failed")
        if not _is_sha256(output.get("sha256")) or not _is_sha256(
            output.get("decoded_rgba_sha256")
        ):
            raise ScreenError(f"{name} PNG hashes are malformed")
        timing = output.get("encoding_wall_s")
        if not isinstance(timing, (int, float)) or not math.isfinite(timing) or timing < 0:
            raise ScreenError(f"{name} encoding timing is invalid")

    diagnostic = report.get("reference_free_diagnostic")
    if not isinstance(diagnostic, dict):
        raise ScreenError("reference-free diagnostic is missing")
    if diagnostic.get("descriptive_only") is not True:
        raise ScreenError("diagnostic must remain descriptive")
    if diagnostic.get("quality_authorized") is not False:
        raise ScreenError("diagnostic cannot authorize quality")
    for key, value in diagnostic.items():
        if isinstance(value, float) and not math.isfinite(value):
            raise ScreenError(f"diagnostic {key} is non-finite")
    feasible = (
        pass_manifest["guided_pair_extractable"]
        and decision.get("same_render_pair_extracted") is True
        and decision.get("guide_passes_extracted") is True
    )
    if decision.get("experimental_feasible") is not feasible:
        raise ScreenError("stored feasibility decision mismatch")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--scene", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--frame", type=int)
    parser.add_argument("--samples", type=int)
    parser.add_argument("--device", choices=("METAL", "CPU"))
    parser.add_argument("--view-layer")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--sample-offset", type=int, default=0)
    parser.add_argument("--warmup-samples", type=int, default=1)
    parser.add_argument("--png-compression", type=int, default=15)
    args = parser.parse_args(argv)
    if args.probe_only:
        forbidden = (
            args.scene,
            args.output_dir,
            args.width,
            args.height,
            args.frame,
            args.samples,
            args.device,
        )
        if any(value is not None for value in forbidden):
            parser.error("probe-only does not accept measurement arguments")
        return args
    missing = [
        name
        for name in ("scene", "output_dir", "width", "height", "frame", "samples", "device")
        if getattr(args, name) is None
    ]
    if missing:
        parser.error("measurement mode requires: " + ", ".join(missing))
    if not args.scene.is_absolute() or not args.output_dir.is_absolute():
        parser.error("scene and output-dir must be absolute")
    if not 1 <= args.width <= MAX_DIMENSION or not 1 <= args.height <= MAX_DIMENSION:
        parser.error("width and height must be in [1,4096]")
    if args.width * args.height > MAX_PIXELS:
        parser.error("pixel count exceeds the bounded screen")
    if not 1 <= args.samples <= MAX_SAMPLES:
        parser.error("samples must be in [1,4096]")
    if not 1 <= args.warmup_samples <= 8:
        parser.error("warmup-samples must be in [1,8]")
    if not -1_000_000 <= args.frame <= 1_000_000:
        parser.error("frame must be in [-1000000,1000000]")
    if not 0 <= args.seed <= MAX_SAMPLE_OFFSET:
        parser.error("seed is outside Cycles' bounded integer range")
    if not 0 <= args.sample_offset <= MAX_SAMPLE_OFFSET:
        parser.error("sample-offset is outside Cycles' bounded integer range")
    if args.sample_offset + args.samples > MAX_SAMPLE_OFFSET:
        parser.error("sample range exceeds Cycles' bounded integer range")
    if not 0 <= args.png_compression <= 100:
        parser.error("png-compression must be in [0,100]")
    return args


def _prepare_new_output_dir(path: Path) -> Path:
    if not path.is_absolute():
        raise ScreenError("output directory must be absolute")
    resolved = path.resolve()
    if resolved.exists():
        raise ScreenError("output directory must not already exist")
    resolved.mkdir(mode=0o700, parents=True)
    if resolved.is_symlink() or not resolved.is_dir():
        raise ScreenError("could not create a regular output directory")
    return resolved


def _require_property(owner: Any, name: str, value: Any) -> None:
    if not hasattr(owner, name):
        raise ScreenError(f"required Blender property is unavailable: {name}")
    try:
        setattr(owner, name, value)
    except BaseException as exc:
        raise ScreenError(f"could not set required Blender property: {name}") from exc
    if getattr(owner, name) != value:
        raise ScreenError(f"Blender did not retain required property: {name}")


def _apply_oidn_policy(scene: Any, view_layer: Any) -> dict[str, Any]:
    for name in (
        "denoiser",
        "denoising_input_passes",
        "denoising_prefilter",
        "denoising_quality",
        "denoising_use_gpu",
    ):
        _require_property(scene.cycles, name, OIDN_POLICY[name])
    _require_property(scene.cycles, "use_denoising", True)
    _require_property(view_layer.cycles, "use_denoising", True)
    _require_property(view_layer.cycles, "denoising_store_passes", True)
    actual = {
        "denoiser": str(scene.cycles.denoiser),
        "denoising_input_passes": str(scene.cycles.denoising_input_passes),
        "denoising_prefilter": str(scene.cycles.denoising_prefilter),
        "denoising_quality": str(scene.cycles.denoising_quality),
        "denoising_use_gpu": bool(scene.cycles.denoising_use_gpu),
        "scene_use_denoising": bool(scene.cycles.use_denoising),
        "view_layer_denoising_store_passes": bool(
            view_layer.cycles.denoising_store_passes
        ),
        "view_layer_use_denoising": bool(view_layer.cycles.use_denoising),
    }
    if actual != OIDN_POLICY:
        raise ScreenError("actual OIDN policy does not match the pinned policy")
    return actual


def _configure_device(bpy: Any, scene: Any, requested: str) -> tuple[str, list[str]]:
    if requested == "CPU":
        scene.cycles.device = "CPU"
        return "CPU", ["CPU"]
    preferences = bpy.context.preferences.addons["cycles"].preferences
    _require_property(preferences, "compute_device_type", "METAL")
    preferences.get_devices()
    if hasattr(preferences, "get_devices_for_type"):
        preferences.get_devices_for_type("METAL")
    metal = [
        device
        for device in preferences.devices
        if getattr(device, "type", "") == "METAL"
    ]
    if not metal:
        raise ScreenError("METAL requested but Cycles enumerated no Metal device")
    for device in preferences.devices:
        device.use = device in metal
    scene.cycles.device = "GPU"
    return "GPU/METAL", sorted(str(device.name) for device in metal)


def _select_view_layer(scene: Any, requested: str | None) -> Any:
    if not scene.view_layers:
        raise ScreenError("scene has no render view layer")
    if requested is None:
        return scene.view_layers[0]
    layer = scene.view_layers.get(requested)
    if layer is None:
        raise ScreenError(f"view layer does not exist: {requested!r}")
    return layer


def _build_stage_graph(
    scene: Any, view_layer: Any, stage_dir: Path
) -> tuple[dict[str, Any], dict[str, str]]:
    scene.use_nodes = True
    nodes = scene.node_tree.nodes
    nodes.clear()
    render_layers = nodes.new("CompositorNodeRLayers")
    render_layers.name = "CX OIDN Source"
    render_layers.label = "CX one-render OIDN source"
    render_layers.layer = view_layer.name
    ordered_passes = [socket.name for socket in render_layers.outputs]
    manifest = analyze_pass_manifest(ordered_passes)
    if not manifest["guided_pair_extractable"]:
        raise ScreenError(
            "required same-render Cycles passes are unavailable: "
            + ", ".join(manifest["missing_required"])
        )

    composite = nodes.new("CompositorNodeComposite")
    composite.name = "CX OIDN Composite"
    scene.node_tree.links.new(render_layers.outputs["Image"], composite.inputs["Image"])

    stage_prefixes: dict[str, str] = {}
    link_manifest: list[dict[str, str]] = []
    for pass_name in REQUIRED_PASSES:
        stage_name = STAGE_BY_PASS[pass_name]
        prefix = f"cx-{stage_name}-"
        stage_prefixes[stage_name] = prefix
        node = nodes.new("CompositorNodeOutputFile")
        node.name = f"CX OIDN {stage_name.title()} Stage"
        node.label = f"CX {pass_name} float stage"
        node.base_path = str(stage_dir)
        node.format.file_format = "OPEN_EXR"
        node.format.color_mode = "RGBA"
        node.format.color_depth = "32"
        node.format.exr_codec = "ZIP"
        node.file_slots[0].path = prefix
        scene.node_tree.links.new(render_layers.outputs[pass_name], node.inputs[0])
        link_manifest.append(
            {
                "destination": f"{node.name}.Image",
                "pass": pass_name,
                "source": f"{render_layers.name}.{pass_name}",
                "stage": stage_name,
            }
        )
    graph = {
        "bypasses_original_compositor": True,
        "exr_color_depth": 32,
        "exr_codec": "ZIP",
        "exr_color_mode": "RGBA",
        "links": link_manifest,
        "links_sha256": _canonical_sha256(link_manifest),
        "source_node": render_layers.name,
        "view_layer": view_layer.name,
    }
    return {"graph": graph, "pass_manifest": manifest}, stage_prefixes


def _discover_stage_files(stage_dir: Path, prefixes: dict[str, str]) -> dict[str, Path]:
    discovered: dict[str, Path] = {}
    for name, prefix in prefixes.items():
        matches = sorted(stage_dir.glob(prefix + "*.exr"))
        if len(matches) != 1:
            raise ScreenError(
                f"expected exactly one {name} EXR from the target render, got {len(matches)}"
            )
        path = matches[0]
        if path.is_symlink() or not path.is_file():
            raise ScreenError(f"{name} EXR is not a regular file")
        discovered[name] = path
    return discovered


def _float_image_attestation(
    bpy: Any,
    path: Path,
    *,
    expected_width: int,
    expected_height: int,
    source_render_id: str,
) -> tuple[dict[str, Any], Any]:
    try:
        import numpy as np  # Blender 4.2 bundles numpy.
    except ImportError as exc:  # pragma: no cover - pinned Blender boundary
        raise ScreenError("pinned Blender lacks numpy for bounded float extraction") from exc
    started = time.perf_counter()
    image = bpy.data.images.load(str(path), check_existing=False)
    if tuple(image.size) != (expected_width, expected_height):
        bpy.data.images.remove(image)
        raise ScreenError(f"{path}: staged EXR dimensions changed")
    if int(image.channels) != 4:
        bpy.data.images.remove(image)
        raise ScreenError(f"{path}: staged EXR is not RGBA")
    values = np.empty(expected_width * expected_height * 4, dtype=np.float32)
    image.pixels.foreach_get(values)
    extraction_wall_s = time.perf_counter() - started
    nonfinite = int(values.size - np.count_nonzero(np.isfinite(values)))
    digest = hashlib.sha256(values.tobytes(order="C")).hexdigest()
    exr_sha, exr_bytes = _sha256_file(path)
    return {
        "channels": 4,
        "colorspace": str(image.colorspace_settings.name),
        "exr_bytes": exr_bytes,
        "exr_path": str(path.name),
        "exr_sha256": exr_sha,
        "extraction_wall_s": _round9(extraction_wall_s),
        "float_count": int(values.size),
        "float_rgba_sha256": digest,
        "height": expected_height,
        "nonfinite_float_count": nonfinite,
        "source_render_id": source_render_id,
        "width": expected_width,
    }, image


def _encode_png_from_float_image(
    image: Any,
    destination: Path,
    scene: Any,
    *,
    compression: int,
    expected_width: int,
    expected_height: int,
    source_render_id: str,
) -> tuple[dict[str, Any], bytes]:
    if destination.exists():
        raise ScreenError(f"PNG destination unexpectedly exists: {destination}")
    image_settings = scene.render.image_settings
    image_settings.file_format = "PNG"
    image_settings.color_mode = "RGBA"
    image_settings.color_depth = "8"
    image_settings.compression = compression
    started = time.perf_counter()
    image.save_render(str(destination), scene=scene)
    encoding_wall_s = time.perf_counter() - started
    rgba, png_info = decode_strict_rgba_png(
        destination,
        expected_width=expected_width,
        expected_height=expected_height,
    )
    file_sha, file_bytes = _sha256_file(destination)
    return {
        "bytes": file_bytes,
        "decoded_rgba_sha256": hashlib.sha256(rgba).hexdigest(),
        "encoding_wall_s": _round9(encoding_wall_s),
        "path": destination.name,
        "sha256": file_sha,
        "source_render_id": source_render_id,
        "strict_png": png_info,
    }, rgba


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    data = (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode(
        "utf-8"
    )
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _blender_identity(bpy: Any) -> dict[str, Any]:
    build_hash = bpy.app.build_hash
    if isinstance(build_hash, bytes):
        build_hash = build_hash.decode("ascii", errors="replace")
    binary = Path(bpy.app.binary_path).resolve(strict=True)
    binary_sha, binary_bytes = _sha256_file(binary)
    return {
        "binary_bytes": binary_bytes,
        "binary_path": str(binary),
        "binary_sha256": binary_sha,
        "build_hash": str(build_hash),
        "version": str(bpy.app.version_string),
        "version_tuple": list(bpy.app.version),
    }


def _color_management(scene: Any) -> dict[str, Any]:
    view = scene.view_settings
    display = scene.display_settings
    return {
        "display_device": str(display.display_device),
        "exposure": float(view.exposure),
        "gamma": float(view.gamma),
        "look": str(view.look),
        "view_transform": str(view.view_transform),
    }


def _probe_only(args: argparse.Namespace) -> dict[str, Any]:
    try:
        import bpy  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - Blender runtime boundary
        raise ScreenError("probe-only must run inside Blender") from exc
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    view_layer = _select_view_layer(scene, args.view_layer)
    actual = _apply_oidn_policy(scene, view_layer)
    scene.use_nodes = True
    node = scene.node_tree.nodes.new("CompositorNodeRLayers")
    node.layer = view_layer.name
    manifest = analyze_pass_manifest(socket.name for socket in node.outputs)
    if not manifest["guided_pair_extractable"]:
        raise ScreenError("Blender cannot expose the required OIDN multipass set")
    return {
        "blender": _blender_identity(bpy),
        "experimental_only": True,
        "kind": KIND,
        "mode": "probe_only_no_render_no_write",
        "oidn_policy": actual,
        "oidn_policy_sha256": _canonical_sha256(actual),
        "pass_manifest": manifest,
        "quality_authorized": False,
        "render_invocations": 0,
        "schema_version": SCHEMA_VERSION,
        "writes": 0,
    }


def _run_blender(args: argparse.Namespace) -> dict[str, Any]:
    try:
        import bpy  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - Blender runtime boundary
        raise ScreenError("measurement must run inside Blender") from exc

    requested_source = args.scene.expanduser()
    if requested_source.is_symlink():
        raise ScreenError("scene must be a regular non-symlink file")
    source = requested_source.resolve(strict=True)
    if not source.is_file():
        raise ScreenError("scene must be a regular non-symlink file")
    source_sha_before, source_bytes = _sha256_file(source)
    current = Path(bpy.data.filepath).resolve() if bpy.data.filepath else None
    if current != source:
        bpy.ops.wm.open_mainfile(filepath=str(source))
    if Path(bpy.data.filepath).resolve(strict=True) != source:
        raise ScreenError("Blender did not open the pinned scene")
    output_dir = _prepare_new_output_dir(args.output_dir)
    stage_dir = output_dir / "float-stages"
    stage_dir.mkdir(mode=0o700)

    scene = bpy.context.scene
    view_layer = _select_view_layer(scene, args.view_layer)
    original_compositor = {
        "node_count": len(scene.node_tree.nodes) if scene.node_tree else 0,
        "node_types": sorted(
            node.bl_idname for node in scene.node_tree.nodes
        ) if scene.node_tree else [],
        "use_compositing": bool(scene.render.use_compositing),
        "use_nodes": bool(scene.use_nodes),
    }
    original_compositor["sha256"] = _canonical_sha256(original_compositor)

    scene.render.engine = "CYCLES"
    scene.render.use_persistent_data = True
    scene.render.resolution_percentage = 100
    scene.render.use_border = False
    scene.render.use_crop_to_border = False
    scene.render.use_multiview = False
    scene.render.use_sequencer = False
    scene.render.use_freestyle = False
    scene.cycles.use_animated_seed = False
    scene.cycles.use_adaptive_sampling = False
    scene.cycles.seed = args.seed
    scene.cycles.sample_offset = args.sample_offset
    actual_device, enabled_devices = _configure_device(bpy, scene, args.device)
    actual_oidn = _apply_oidn_policy(scene, view_layer)
    scene.frame_set(args.frame)

    # One bounded warmup renders the same scene/device/frame at 1x1.  It warms
    # kernel, OIDN, and resident-scene state but is not a target-resolution arm.
    scene.render.use_compositing = False
    scene.render.resolution_x = 1
    scene.render.resolution_y = 1
    scene.cycles.samples = args.warmup_samples
    bpy.context.view_layer.update()
    warmup_started = time.perf_counter()
    bpy.ops.render.render(write_still=False, layer=view_layer.name)
    warmup_wall_s = time.perf_counter() - warmup_started

    scene.render.resolution_x = args.width
    scene.render.resolution_y = args.height
    scene.cycles.samples = args.samples
    scene.cycles.seed = args.seed
    scene.cycles.sample_offset = args.sample_offset
    scene.render.use_compositing = True
    graph_result, prefixes = _build_stage_graph(scene, view_layer, stage_dir)
    pass_manifest = graph_result["pass_manifest"]
    graph = graph_result["graph"]

    exact_target_settings = {
        "device": actual_device,
        "enabled_device_names": enabled_devices,
        "frame": args.frame,
        "height": args.height,
        "oidn_policy": actual_oidn,
        "persistent_data": True,
        "sample_offset": args.sample_offset,
        "sample_range": [args.sample_offset, args.sample_offset + args.samples],
        "samples": args.samples,
        "seed": args.seed,
        "use_adaptive_sampling": False,
        "view_layer": view_layer.name,
        "width": args.width,
    }
    settings_sha = _canonical_sha256(exact_target_settings)
    source_render_id_value = source_render_id(
        graph_sha256=graph["links_sha256"],
        pass_manifest_sha256=pass_manifest["ordered_socket_names_sha256"],
        scene_sha256=source_sha_before,
        settings_sha256=settings_sha,
    )

    bpy.context.view_layer.update()
    target_started = time.perf_counter()
    bpy.ops.render.render(write_still=False, layer=view_layer.name)
    target_wall_s = time.perf_counter() - target_started
    stage_paths = _discover_stage_files(stage_dir, prefixes)

    stages: dict[str, Any] = {}
    outputs: dict[str, Any] = {}
    decoded: dict[str, bytes] = {}
    for stage_name in ("denoised", "noisy", "albedo", "normal"):
        stage, image = _float_image_attestation(
            bpy,
            stage_paths[stage_name],
            expected_width=args.width,
            expected_height=args.height,
            source_render_id=source_render_id_value,
        )
        stages[stage_name] = stage
        try:
            if stage_name in {"denoised", "noisy"}:
                output, rgba = _encode_png_from_float_image(
                    image,
                    output_dir / f"{stage_name}.png",
                    scene,
                    compression=args.png_compression,
                    expected_width=args.width,
                    expected_height=args.height,
                    source_render_id=source_render_id_value,
                )
                outputs[stage_name] = output
                decoded[stage_name] = rgba
        finally:
            bpy.data.images.remove(image)

    diagnostic_started = time.perf_counter()
    diagnostic = reference_free_diagnostic(decoded["denoised"], decoded["noisy"])
    diagnostic["wall_s"] = _round9(time.perf_counter() - diagnostic_started)
    decision = {
        "experimental_feasible": True,
        "guide_passes_extracted": all(
            stages[name]["source_render_id"] == source_render_id_value
            for name in ("albedo", "normal")
        ),
        "production_change_authorized": False,
        "quality_authorized": False,
        "same_render_pair_extracted": all(
            stages[name]["source_render_id"] == source_render_id_value
            and outputs[name]["source_render_id"] == source_render_id_value
            for name in ("denoised", "noisy")
        ),
    }
    decision["experimental_feasible"] = bool(
        pass_manifest["guided_pair_extractable"]
        and decision["guide_passes_extracted"]
        and decision["same_render_pair_extracted"]
    )

    source_sha_after, source_bytes_after = _sha256_file(source)
    if source_sha_after != source_sha_before or source_bytes_after != source_bytes:
        raise ScreenError("source scene changed during the read-only screen")
    report = {
        "blender": _blender_identity(bpy),
        "configuration": {
            "color_management": _color_management(scene),
            "compositor_graph": graph,
            "exact_target_settings": exact_target_settings,
            "exact_target_settings_sha256": settings_sha,
            "original_compositor": original_compositor,
            "png_compression": args.png_compression,
            "warmup": {
                "frame": args.frame,
                "height": 1,
                "oidn_policy": actual_oidn,
                "samples": args.warmup_samples,
                "width": 1,
            },
        },
        "decision": decision,
        "experimental_only": True,
        "host": {
            "machine": platform.machine(),
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "kind": KIND,
        "limitations": [
            "the reference-free noisy/denoised diagnostic does not establish image quality",
            "one scene/frame/host cannot authorize a verifier or production backend change",
            "the original scene compositor is bypassed, so compositor-heavy scenes need separate coverage",
            "target render time includes four 32-bit ZIP EXR compositor staging writes",
            "the 1x1 warmup is a separate non-target render invocation",
        ],
        "outputs": outputs,
        "pass_manifest": pass_manifest,
        "reference_free_diagnostic": diagnostic,
        "render": {
            "source_render_id": source_render_id_value,
            "target_render_and_exr_staging_wall_s": _round9(target_wall_s),
            "target_render_invocations": 1,
            "target_timer_includes_exr_staging": True,
            "warmup_render_invocations": 1,
            "warmup_wall_s": _round9(warmup_wall_s),
        },
        "scene": {
            "bytes": source_bytes,
            "path": str(source),
            "sha256": source_sha_before,
        },
        "schema_version": SCHEMA_VERSION,
        "stages": stages,
    }
    validate_report(report)
    _write_json_atomic(output_dir / REPORT_NAME, report)
    return report


def _cli_argv(argv: list[str]) -> list[str]:
    if "--" in argv:
        return argv[argv.index("--") + 1 :]
    return argv[1:]


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv if argv is None else argv
    try:
        args = _parse_args(_cli_argv(raw))
        if args.probe_only:
            result = _probe_only(args)
        else:
            report = _run_blender(args)
            result = {
                "decision": report["decision"],
                "kind": KIND,
                "ok": True,
                "report": REPORT_NAME,
                "schema_version": SCHEMA_VERSION,
            }
    except BaseException as exc:
        result = {
            "error": f"{type(exc).__name__}: {exc}"[:1000],
            "kind": KIND,
            "ok": False,
            "schema_version": SCHEMA_VERSION,
        }
        print(json.dumps(result, sort_keys=True), file=sys.stderr, flush=True)
        return 2
    result.setdefault("ok", True)
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
