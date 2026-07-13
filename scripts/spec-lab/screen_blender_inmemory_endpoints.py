#!/usr/bin/env python3
"""Bounded Koro screen for render-to-memory and raster fixed-overhead limits.

The normal host mode launches the pinned Blender executable once.  The child
mode opens a read-only scene, warms resident renderer state, and measures:

* Cycles ``bpy.ops.render.render(write_still=True)`` at 1 and 4 SPP;
* Cycles ``write_still=False`` followed by bounded Render Result float extraction
  and a separately timed ``Image.save_render`` PNG encode at 1 and 4 SPP;
* full-resolution EEVEE Next at 1 and 4 temporal render samples; and
* one full-resolution Workbench raster render.

The private ``_cycles`` session API is inspected and pinned but is never called
unless a stable RenderEngine owner can be established.  Blender 4.2's private
``create`` call binds a native session to a RenderEngine pointer; this screen
therefore fails closed instead of retaining a session beyond that owner's
lifetime.

After Blender exits, host mode evaluates every unique successful PNG with the
existing quality-v3 contract against a retained reference.  It never renders a
new reference.  All results are experimental, local, and unattested.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import stat
import struct
import subprocess
import sys
import time
from typing import Any, Sequence


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

DEFAULT_BLENDER = Path("/Applications/Blender.app/Contents/MacOS/Blender")
DEFAULT_REFERENCE = Path(
    "/Users/scammermike/.cache/cx-spec-lab/transfer/"
    "koro-portrait-1080x1920-r4096-d4-v4-f9-20260712/"
    "cycles-preview-4c50873d7e61bbd6b6afdc10913938de/units/"
    "unit-20b9601a46a3394b1f207f0d9a8e2abb2bf2af4267233bcb2bdd03b6649e901a/"
    "baseline.png"
)

KIND = "cx_blender_inmemory_endpoint_screen"
CHILD_KIND = "cx_blender_inmemory_endpoint_child"
SCHEMA_VERSION = 1
CHILD_REPORT_NAME = "child-render-report.json"
FINAL_REPORT_NAME = "inmemory-endpoint-screen.json"
BASELINE_SECONDS = 112.396726
WIDTH = 1080
HEIGHT = 1920
FRAME = 9
SAMPLE_COUNTS = (1, 4)
MAX_PIXELS = WIDTH * HEIGHT
MAX_PNG_BYTES = 64 * 1024 * 1024
MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
MAX_CAPTURE_BYTES = MAX_PIXELS * 4 * 4
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ScreenError(ValueError):
    """A fail-closed experimental-screen error."""


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ScreenError("report is not finite canonical JSON") from exc


def _round9(value: float) -> float:
    if not math.isfinite(value) or value < 0.0:
        raise ScreenError("timing is negative or non-finite")
    return round(float(value), 9)


def _sha256_file(path: Path, *, maximum: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    raise ScreenError(f"{path.name} exceeds its byte bound")
                digest.update(chunk)
    except OSError as exc:
        raise ScreenError(f"cannot read {path.name}") from exc
    if total <= 0:
        raise ScreenError(f"{path.name} is empty")
    return digest.hexdigest(), total


def _regular_identity(path: Path) -> tuple[int, ...]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ScreenError(f"cannot stat {path}") from exc
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise ScreenError(f"{path} must be a regular non-symlink file")
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
        info.st_gid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _png_identity(path: Path, *, width: int, height: int) -> dict[str, Any]:
    before = _regular_identity(path)
    digest, byte_count = _sha256_file(path, maximum=MAX_PNG_BYTES)
    try:
        with path.open("rb") as handle:
            prefix = handle.read(33)
    except OSError as exc:
        raise ScreenError("cannot inspect PNG") from exc
    if len(prefix) != 33 or prefix[:8] != PNG_SIGNATURE:
        raise ScreenError("render artifact is not a PNG")
    if struct.unpack(">I", prefix[8:12])[0] != 13 or prefix[12:16] != b"IHDR":
        raise ScreenError("render artifact has no canonical IHDR")
    actual_width, actual_height, depth, color, compression, filtering, interlace = (
        struct.unpack(">IIBBBBB", prefix[16:29])
    )
    if (actual_width, actual_height) != (width, height):
        raise ScreenError("render artifact dimensions mismatch")
    if depth != 8 or color not in {2, 6}:
        raise ScreenError("render artifact is not RGB/RGBA 8-bit")
    if compression != 0 or filtering != 0 or interlace not in {0, 1}:
        raise ScreenError("render artifact uses unsupported PNG methods")
    if _regular_identity(path) != before:
        raise ScreenError("render artifact changed during inspection")
    return {
        "bytes": byte_count,
        "color_type": color,
        "height": actual_height,
        "path": path.name,
        "sha256": digest,
        "width": actual_width,
    }


def _write_new_json(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(
        value,
        sort_keys=True,
        indent=2,
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii") + b"\n"
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise ScreenError("JSON parent must be an existing non-symlink directory")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ScreenError(f"cannot create {path.name}") from exc
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)


def _safe_output_path(root: Path, name: str) -> Path:
    if (
        not name
        or len(name) > 128
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-_." for character in name)
        or "/" in name
        or "\\" in name
    ):
        raise ScreenError("unsafe artifact name")
    path = root / name
    if path.exists() or path.is_symlink():
        raise ScreenError("refusing to replace an artifact")
    return path


def _prepare_output_root(path: Path) -> Path:
    if not path.is_absolute():
        raise ScreenError("output root must be absolute")
    if path.exists() or path.is_symlink():
        raise ScreenError("output root must not already exist")
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise ScreenError("output parent must be an existing non-symlink directory")
    path.mkdir(mode=0o700)
    return path


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--scene", required=True, type=Path)
    parser.add_argument("--reference", type=Path, default=DEFAULT_REFERENCE)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--blender", type=Path, default=DEFAULT_BLENDER)
    parser.add_argument("--frame", type=int, default=FRAME)
    parser.add_argument("--device", choices=("METAL", "CPU"), default="METAL")
    parser.add_argument("--timeout-secs", type=int, default=600)
    return parser.parse_args(argv)


def _cli_argv(argv: Sequence[str] | None) -> list[str]:
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--" in raw:
        return raw[raw.index("--") + 1 :]
    return raw


def _validate_common_args(args: argparse.Namespace, *, child: bool) -> None:
    for label in ("scene", "reference"):
        path = getattr(args, label)
        if not path.is_absolute():
            raise ScreenError(f"--{label} must be absolute")
        _regular_identity(path)
    if args.scene.suffix != ".blend":
        raise ScreenError("scene must be a lowercase .blend file")
    if not args.output_root.is_absolute():
        raise ScreenError("output root must be absolute")
    if not child:
        if not args.blender.is_absolute() or not os.access(args.blender, os.X_OK):
            raise ScreenError("Blender must be an absolute executable")
        if args.output_root.exists() or args.output_root.is_symlink():
            raise ScreenError("output root must not already exist")
    if not 0 <= args.frame <= 1_000_000:
        raise ScreenError("frame is outside the bounded range")
    if not 1 <= args.timeout_secs <= 600:
        raise ScreenError("timeout must be in [1,600]")


def _configure_cycles_device(bpy: Any, scene: Any, requested: str) -> dict[str, Any]:
    if requested == "CPU":
        scene.cycles.device = "CPU"
        return {"actual": "CPU", "enabled": ["CPU"]}
    preferences = bpy.context.preferences.addons["cycles"].preferences
    preferences.compute_device_type = "METAL"
    preferences.get_devices()
    if hasattr(preferences, "get_devices_for_type"):
        preferences.get_devices_for_type("METAL")
    metal = [
        device
        for device in preferences.devices
        if getattr(device, "type", "") == "METAL"
    ]
    if not metal:
        raise ScreenError("METAL requested but no Metal device was enumerated")
    for device in preferences.devices:
        device.use = device in metal
    scene.cycles.device = "GPU"
    return {
        "actual": "GPU/METAL",
        "enabled": sorted(str(device.name) for device in metal),
    }


def _configure_output(scene: Any) -> None:
    scene.render.resolution_x = WIDTH
    scene.render.resolution_y = HEIGHT
    scene.render.resolution_percentage = 100
    scene.render.use_border = False
    scene.render.use_crop_to_border = False
    scene.render.use_file_extension = True
    scene.render.use_overwrite = True
    scene.render.use_multiview = False
    scene.render.use_compositing = False
    scene.render.use_sequencer = False
    scene.render.use_freestyle = False
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.image_settings.color_depth = "8"
    scene.render.image_settings.compression = 0


def _configure_cycles(scene: Any, samples: int, seed: int) -> None:
    scene.render.engine = "CYCLES"
    scene.cycles.samples = samples
    scene.cycles.seed = seed
    scene.cycles.sample_offset = 0
    scene.cycles.use_animated_seed = False
    scene.cycles.use_adaptive_sampling = False
    scene.cycles.use_denoising = False
    scene.cycles.pixel_filter_type = "BLACKMAN_HARRIS"
    scene.cycles.filter_width = 1.5
    scene.render.filter_size = 1.5
    scene.render.use_persistent_data = True
    for layer in scene.view_layers:
        if hasattr(layer.cycles, "use_denoising"):
            layer.cycles.use_denoising = False


def _render_still_arm(
    bpy: Any,
    scene: Any,
    output_root: Path,
    *,
    arm: str,
    samples: int | None,
) -> dict[str, Any]:
    destination = _safe_output_path(output_root, f"{arm}.png")
    total_started = time.perf_counter()
    scene.render.filepath = str(destination)
    update_started = time.perf_counter()
    bpy.context.view_layer.update()
    update_s = time.perf_counter() - update_started
    render_started = time.perf_counter()
    bpy.ops.render.render(write_still=True)
    render_s = time.perf_counter() - render_started
    inspect_started = time.perf_counter()
    artifact = _png_identity(destination, width=WIDTH, height=HEIGHT)
    inspect_s = time.perf_counter() - inspect_started
    total_s = time.perf_counter() - total_started
    return {
        "arm": arm,
        "artifact": artifact,
        "engine": str(scene.render.engine),
        "samples": samples,
        "supported": True,
        "timings_s": {
            "artifact_validation_hash": _round9(inspect_s),
            "render_operator_including_encode": _round9(render_s),
            "total_usable_encoded": _round9(total_s),
            "view_layer_update": _round9(update_s),
        },
    }


def _render_result_arm(
    bpy: Any,
    np: Any,
    scene: Any,
    output_root: Path,
    *,
    arm: str,
    samples: int,
) -> dict[str, Any]:
    destination = _safe_output_path(output_root, f"{arm}.png")
    total_started = time.perf_counter()
    update_started = time.perf_counter()
    bpy.context.view_layer.update()
    update_s = time.perf_counter() - update_started
    render_started = time.perf_counter()
    bpy.ops.render.render(write_still=False)
    render_s = time.perf_counter() - render_started

    extraction_started = time.perf_counter()
    result = bpy.data.images.get("Render Result")
    if result is None or tuple(result.size) != (WIDTH, HEIGHT):
        raise ScreenError("Render Result is missing or has the wrong dimensions")
    expected_values = WIDTH * HEIGHT * 4
    if expected_values * 4 != MAX_CAPTURE_BYTES:
        raise ScreenError("Render Result capture bound mismatch")
    pixels = np.empty(expected_values, dtype=np.float32)
    result.pixels.foreach_get(pixels)
    if pixels.shape != (expected_values,) or pixels.nbytes != MAX_CAPTURE_BYTES:
        raise ScreenError("Render Result float buffer has the wrong size")
    if not bool(np.isfinite(pixels).all()):
        raise ScreenError("Render Result contains non-finite pixels")
    pixel_bytes = pixels.tobytes(order="C")
    raw_sha = hashlib.sha256(pixel_bytes).hexdigest()
    extraction_s = time.perf_counter() - extraction_started
    handoff_s = time.perf_counter() - total_started

    encode_started = time.perf_counter()
    # Blender exposes no supported in-memory PNG encoder for Render Result.
    # save_render is therefore timed separately after the immutable float
    # handoff. It encodes the same resident Render Result with scene color
    # management and does not render again.
    result.save_render(filepath=str(destination), scene=scene)
    encode_s = time.perf_counter() - encode_started
    inspect_started = time.perf_counter()
    artifact = _png_identity(destination, width=WIDTH, height=HEIGHT)
    inspect_s = time.perf_counter() - inspect_started
    encoded_s = time.perf_counter() - total_started
    return {
        "arm": arm,
        "artifact": artifact,
        "engine": str(scene.render.engine),
        "render_result": {
            "dtype": "float32",
            "finite": True,
            "height": HEIGHT,
            "nbytes": len(pixel_bytes),
            "sha256": raw_sha,
            "values": expected_values,
            "width": WIDTH,
        },
        "samples": samples,
        "supported": True,
        "timings_s": {
            "artifact_validation_hash": _round9(inspect_s),
            "png_encode_save_render": _round9(encode_s),
            "render_operator_without_encode": _round9(render_s),
            "render_result_float_handoff": _round9(extraction_s),
            "total_usable_encoded": _round9(encoded_s),
            "total_usable_float_handoff": _round9(handoff_s),
            "view_layer_update": _round9(update_s),
        },
    }


def _private_cycles_audit() -> dict[str, Any]:
    import inspect

    try:
        import _cycles  # type: ignore[import-not-found]
        from cycles import engine as cycles_engine  # type: ignore[import-not-found]
    except ImportError as exc:
        return {
            "error": f"{type(exc).__name__}: {exc}"[:500],
            "invoked": False,
            "status": "unavailable",
            "supported": False,
        }
    wrappers = {
        name: inspect.getsource(getattr(cycles_engine, name))
        for name in ("create", "free", "render", "reset", "sync")
    }
    wrapper_path = Path(cycles_engine.__file__).resolve(strict=True)
    wrapper_sha, wrapper_bytes = _sha256_file(wrapper_path, maximum=4 * 1024 * 1024)
    return {
        "available_symbols": sorted(
            name for name in dir(_cycles) if not name.startswith("__")
        ),
        "engine_wrapper": {
            "bytes": wrapper_bytes,
            "path": str(wrapper_path),
            "sha256": wrapper_sha,
            "wrapper_source_sha256": {
                name: hashlib.sha256(source.encode("utf-8")).hexdigest()
                for name, source in wrappers.items()
            },
        },
        "invoked": False,
        "reason": (
            "Blender 4.2 _cycles.create requires a live RenderEngine pointer; "
            "the native session reports results through that owner. Reusing it "
            "outside the operator-owned engine lifetime is not safely callable, "
            "and calling reset/sync/render cannot remove the required owner."
        ),
        "status": "private_unsupported_fail_closed",
        "supported": False,
    }


def _try_raster_arms(
    bpy: Any, scene: Any, output_root: Path
) -> list[dict[str, Any]]:
    arms: list[dict[str, Any]] = []
    try:
        scene.render.engine = "BLENDER_EEVEE_NEXT"
        if not hasattr(scene, "eevee") or not hasattr(
            scene.eevee, "taa_render_samples"
        ):
            raise ScreenError("EEVEE render-sample control is unavailable")
        scene.eevee.taa_render_samples = 4
        bpy.context.view_layer.update()
        bpy.ops.render.render(write_still=False)
        for samples in SAMPLE_COUNTS:
            scene.eevee.taa_render_samples = samples
            bpy.context.view_layer.update()
            bpy.ops.render.render(write_still=False)
            arms.append(
                _render_still_arm(
                    bpy,
                    scene,
                    output_root,
                    arm=f"eevee-next-s{samples}",
                    samples=samples,
                )
            )
    except BaseException as exc:
        arms.append(
            {
                "arm": "eevee-next",
                "error": f"{type(exc).__name__}: {exc}"[:500],
                "engine": "BLENDER_EEVEE_NEXT",
                "supported": False,
            }
        )

    try:
        scene.render.engine = "BLENDER_WORKBENCH"
        bpy.context.view_layer.update()
        bpy.ops.render.render(write_still=False)
        arms.append(
            _render_still_arm(
                bpy,
                scene,
                output_root,
                arm="workbench-full",
                samples=None,
            )
        )
    except BaseException as exc:
        arms.append(
            {
                "arm": "workbench-full",
                "error": f"{type(exc).__name__}: {exc}"[:500],
                "engine": "BLENDER_WORKBENCH",
                "supported": False,
            }
        )
    return arms


def _child_main(args: argparse.Namespace) -> int:
    _validate_common_args(args, child=True)
    try:
        import bpy  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ScreenError("child mode requires Blender with NumPy") from exc

    source_identity_before = _regular_identity(args.scene)
    source_sha, source_bytes = _sha256_file(args.scene, maximum=MAX_SOURCE_BYTES)
    output_root = _prepare_output_root(args.output_root)
    current = Path(bpy.data.filepath).resolve() if bpy.data.filepath else None
    if current != args.scene:
        bpy.ops.wm.open_mainfile(filepath=str(args.scene))
    if Path(bpy.data.filepath).resolve(strict=True) != args.scene:
        raise ScreenError("Blender did not open the requested source")

    scene = bpy.context.scene
    _configure_output(scene)
    device = _configure_cycles_device(bpy, scene, args.device)
    scene.frame_set(args.frame)
    seed = int(source_sha[:8], 16) & 0x7FFFFFFF
    native_integrator = {
        key: int(getattr(scene.cycles, key))
        for key in (
            "max_bounces",
            "diffuse_bounces",
            "glossy_bounces",
            "transmission_bounces",
        )
    }

    # One uncharged full-path warmup establishes resident scene, BVH, Metal
    # kernels, Render Result allocation, and normal filesystem caches.
    _configure_cycles(scene, 4, seed)
    warmup_started = time.perf_counter()
    bpy.context.view_layer.update()
    bpy.ops.render.render(write_still=False)
    warmup_s = time.perf_counter() - warmup_started

    arms: list[dict[str, Any]] = []
    per_sample_warmups: list[dict[str, Any]] = []
    for samples in SAMPLE_COUNTS:
        _configure_cycles(scene, samples, seed)
        sample_warmup_started = time.perf_counter()
        bpy.context.view_layer.update()
        bpy.ops.render.render(write_still=False)
        per_sample_warmups.append(
            {
                "charged": False,
                "engine": "CYCLES",
                "samples": samples,
                "wall_s": _round9(time.perf_counter() - sample_warmup_started),
                "write_still": False,
            }
        )
        standard_name = f"cycles-standard-still-s{samples}"
        try:
            _configure_cycles(scene, samples, seed)
            arms.append(
                _render_still_arm(
                    bpy,
                    scene,
                    output_root,
                    arm=standard_name,
                    samples=samples,
                )
            )
        except BaseException as exc:
            arms.append(
                {
                    "arm": standard_name,
                    "engine": "CYCLES",
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                    "samples": samples,
                    "supported": False,
                }
            )
        memory_name = f"cycles-render-result-s{samples}"
        try:
            _configure_cycles(scene, samples, seed)
            arms.append(
                _render_result_arm(
                    bpy,
                    np,
                    scene,
                    output_root,
                    arm=memory_name,
                    samples=samples,
                )
            )
        except BaseException as exc:
            arms.append(
                {
                    "arm": memory_name,
                    "engine": "CYCLES",
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                    "samples": samples,
                    "supported": False,
                }
            )

    private_cycles = _private_cycles_audit()
    raster_arms = _try_raster_arms(bpy, scene, output_root)
    arms.extend(raster_arms)

    if _regular_identity(args.scene) != source_identity_before:
        raise ScreenError("source scene identity changed during the screen")
    source_sha_after, source_bytes_after = _sha256_file(
        args.scene, maximum=MAX_SOURCE_BYTES
    )
    if source_sha_after != source_sha or source_bytes_after != source_bytes:
        raise ScreenError("source scene bytes changed during the screen")
    build_hash = bpy.app.build_hash
    if isinstance(build_hash, bytes):
        build_hash = build_hash.decode("ascii", errors="replace")
    report = {
        "arms": arms,
        "blender": {
            "build_hash": str(build_hash),
            "version": str(bpy.app.version_string),
        },
        "configuration": {
            "device": device,
            "frame": args.frame,
            "height": HEIGHT,
            "native_integrator": native_integrator,
            "png_compression": 0,
            "sample_counts": list(SAMPLE_COUNTS),
            "seed": seed,
            "use_persistent_data": True,
            "width": WIDTH,
        },
        "experimental_only": True,
        "kind": CHILD_KIND,
        "limitations": [
            "save_render encodes the resident Render Result, not the copied float array",
            "private _cycles calls are unsupported and deliberately not invoked",
            "one warm resident scene/frame/host does not establish generality",
            "EEVEE and Workbench change only the loaded in-memory scene and are not saved",
        ],
        "private_cycles": private_cycles,
        "schema_version": SCHEMA_VERSION,
        "source": {
            "bytes": source_bytes,
            "path": str(args.scene),
            "sha256": source_sha,
            "unchanged": True,
        },
        "warmup": {
            "charged": False,
            "engine": "CYCLES",
            "per_sample": per_sample_warmups,
            "samples": 4,
            "wall_s": _round9(warmup_s),
            "write_still": False,
        },
    }
    _canonical_json(report)
    _write_new_json(output_root / CHILD_REPORT_NAME, report)
    print(
        json.dumps(
            {
                "arms": len(arms),
                "kind": CHILD_KIND,
                "ok": True,
                "report": CHILD_REPORT_NAME,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda token: (_ for _ in ()).throw(
                ScreenError(f"non-finite JSON constant {token}")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScreenError(f"cannot read {path.name}") from exc
    if not isinstance(value, dict):
        raise ScreenError("report root must be an object")
    return value


def _quality_summary(result: dict[str, Any]) -> dict[str, Any]:
    black = result.get("mattes", {}).get("black", {})
    metrics = black.get("metrics", {}) if isinstance(black, dict) else {}
    selected = {}
    for key in (
        "global_rgb_agreement",
        "worst_regional_rgb_agreement",
        "worst_microtile_rgb_agreement",
        "gaussian_luma_ssim",
        "regional_ssim_p5",
        "sobel_gms_mean",
        "sobel_gmsd",
        "flat_high_pass_rmse",
        "sobel_gradient_energy_ratio",
        "haar_detail_cosine",
        "haar_detail_rms_gain",
    ):
        row = metrics.get(key)
        if isinstance(row, dict):
            selected[key] = {field: row.get(field) for field in ("value", "pass")}
    alpha = result.get("alpha_agreement", {})
    return {
        "alpha_agreement": (
            {field: alpha.get(field) for field in ("value", "pass")}
            if isinstance(alpha, dict)
            else None
        ),
        "errors": result.get("errors"),
        "failures": result.get("failures"),
        "metrics_black_matte": selected,
        "pass": result.get("pass") is True,
    }


def _artifact_path(root: Path, artifact: dict[str, Any]) -> Path:
    name = artifact.get("path")
    if not isinstance(name, str):
        raise ScreenError("artifact path is missing")
    relative = PurePosixPath(name)
    if (
        relative.is_absolute()
        or len(relative.parts) != 1
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ScreenError("artifact path is unsafe")
    path = root / Path(*relative.parts)
    identity = _png_identity(path, width=WIDTH, height=HEIGHT)
    if identity["sha256"] != artifact.get("sha256"):
        raise ScreenError("artifact SHA changed after child completion")
    return path


def _host_main(args: argparse.Namespace) -> int:
    _validate_common_args(args, child=False)
    scene_sha_before, scene_bytes = _sha256_file(
        args.scene, maximum=MAX_SOURCE_BYTES
    )
    reference = _png_identity(args.reference, width=WIDTH, height=HEIGHT)
    command = [
        str(args.blender),
        "--background",
        "--factory-startup",
        "--disable-autoexec",
        "--python",
        str(Path(__file__).resolve()),
        "--",
        "--child",
        "--scene",
        str(args.scene),
        "--reference",
        str(args.reference),
        "--output-root",
        str(args.output_root),
        "--blender",
        str(args.blender),
        "--frame",
        str(args.frame),
        "--device",
        args.device,
        "--timeout-secs",
        str(args.timeout_secs),
    ]
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=args.timeout_secs,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ScreenError("Blender child exceeded the timeout") from exc
    child_wall_s = time.perf_counter() - started
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace")[-4000:]
        raise ScreenError(f"Blender child failed: {stderr}")
    child_report = _load_json(args.output_root / CHILD_REPORT_NAME)
    if (
        child_report.get("kind") != CHILD_KIND
        or child_report.get("schema_version") != SCHEMA_VERSION
        or child_report.get("source", {}).get("sha256") != scene_sha_before
    ):
        raise ScreenError("child report identity mismatch")

    try:
        import cx_render_quality_v3 as quality
    except ImportError as exc:
        raise ScreenError("quality-v3 module is unavailable") from exc
    quality_by_sha: dict[str, dict[str, Any]] = {}
    quality_files: dict[str, str] = {}
    for arm in child_report.get("arms", []):
        if not isinstance(arm, dict) or arm.get("supported") is not True:
            continue
        artifact = arm.get("artifact")
        if not isinstance(artifact, dict):
            continue
        candidate = _artifact_path(args.output_root, artifact)
        digest = artifact["sha256"]
        if digest in quality_by_sha:
            continue
        result = quality.evaluate_pngs(
            candidate, args.reference, target_size=(WIDTH, HEIGHT)
        )
        quality_name = f"quality-v3-{digest[:20]}.json"
        _write_new_json(args.output_root / quality_name, result)
        quality_by_sha[digest] = result
        quality_files[digest] = quality_name

    arms: list[dict[str, Any]] = []
    for original in child_report.get("arms", []):
        if not isinstance(original, dict):
            raise ScreenError("child arm is malformed")
        arm = dict(original)
        artifact = arm.get("artifact")
        if isinstance(artifact, dict) and artifact.get("sha256") in quality_by_sha:
            digest = artifact["sha256"]
            arm["quality_v3"] = {
                "full_result_path": quality_files[digest],
                "summary": _quality_summary(quality_by_sha[digest]),
            }
        timings = arm.get("timings_s")
        if isinstance(timings, dict) and isinstance(
            timings.get("total_usable_encoded"), (int, float)
        ):
            endpoint = float(timings["total_usable_encoded"])
            arm["speedup_vs_4096_baseline_x"] = round(
                BASELINE_SECONDS / endpoint, 6
            )
        arms.append(arm)

    encoded = [
        arm
        for arm in arms
        if arm.get("supported") is True
        and isinstance(arm.get("timings_s"), dict)
        and isinstance(arm["timings_s"].get("total_usable_encoded"), (int, float))
    ]
    encoded.sort(key=lambda arm: arm["timings_s"]["total_usable_encoded"])
    passing = [
        arm
        for arm in encoded
        if arm.get("quality_v3", {}).get("summary", {}).get("pass") is True
    ]
    handoffs = [
        arm
        for arm in arms
        if isinstance(arm.get("timings_s"), dict)
        and isinstance(
            arm["timings_s"].get("total_usable_float_handoff"), (int, float)
        )
    ]
    handoffs.sort(key=lambda arm: arm["timings_s"]["total_usable_float_handoff"])
    cutoff = BASELINE_SECONDS / 1000.0
    decision = {
        "baseline_seconds": BASELINE_SECONDS,
        "encoded_1000x_cutoff_s": round(cutoff, 9),
        "lowest_encoded_arm": encoded[0]["arm"] if encoded else None,
        "lowest_encoded_s": (
            encoded[0]["timings_s"]["total_usable_encoded"] if encoded else None
        ),
        "lowest_float_handoff_arm": handoffs[0]["arm"] if handoffs else None,
        "lowest_float_handoff_s": (
            handoffs[0]["timings_s"]["total_usable_float_handoff"]
            if handoffs
            else None
        ),
        "lowest_quality_passing_encoded_arm": passing[0]["arm"] if passing else None,
        "lowest_quality_passing_encoded_s": (
            passing[0]["timings_s"]["total_usable_encoded"] if passing else None
        ),
        "quality_passing_encoded_count": len(passing),
        "unique_quality_audits": len(quality_by_sha),
    }
    decision["any_quality_passing_encoded_at_1000x"] = bool(
        passing
        and passing[0]["timings_s"]["total_usable_encoded"] <= cutoff
    )
    by_name = {arm.get("arm"): arm for arm in arms}
    operator_comparisons: list[dict[str, Any]] = []
    for samples in SAMPLE_COUNTS:
        standard = by_name.get(f"cycles-standard-still-s{samples}", {})
        memory = by_name.get(f"cycles-render-result-s{samples}", {})
        standard_artifact = standard.get("artifact", {})
        memory_artifact = memory.get("artifact", {})
        operator_comparisons.append(
            {
                "samples": samples,
                "standard_arm": standard.get("arm"),
                "render_result_arm": memory.get("arm"),
                "encoded_png_sha_equal": bool(
                    isinstance(standard_artifact, dict)
                    and isinstance(memory_artifact, dict)
                    and standard_artifact.get("sha256") is not None
                    and standard_artifact.get("sha256")
                    == memory_artifact.get("sha256")
                ),
                "dimensions_equal": bool(
                    isinstance(standard_artifact, dict)
                    and isinstance(memory_artifact, dict)
                    and (
                        standard_artifact.get("width"),
                        standard_artifact.get("height"),
                    )
                    == (
                        memory_artifact.get("width"),
                        memory_artifact.get("height"),
                    )
                ),
            }
        )
    final = {
        "arms": arms,
        "baseline_reference": {
            **reference,
            "baseline_seconds": BASELINE_SECONDS,
            "path": str(args.reference),
            "reused_existing_4096": True,
        },
        "blender": child_report["blender"],
        "child_process": {
            "returncode": completed.returncode,
            "stderr_sha256": hashlib.sha256(completed.stderr).hexdigest(),
            "stdout_sha256": hashlib.sha256(completed.stdout).hexdigest(),
            "wall_s": _round9(child_wall_s),
        },
        "configuration": child_report["configuration"],
        "cycles_operator_comparisons": operator_comparisons,
        "decision": decision,
        "experimental_only": True,
        "host": {
            "machine": platform.machine(),
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "kind": KIND,
        "limitations": child_report["limitations"],
        "private_cycles": child_report["private_cycles"],
        "quality_contract": {
            "id": quality.CONTRACT_ID,
            "module_sha256": _sha256_file(
                Path(quality.__file__).resolve(strict=True), maximum=4 * 1024 * 1024
            )[0],
            "measurement_only": True,
        },
        "schema_version": SCHEMA_VERSION,
        "source": child_report["source"],
        "warmup": child_report["warmup"],
    }
    scene_sha_after, scene_bytes_after = _sha256_file(
        args.scene, maximum=MAX_SOURCE_BYTES
    )
    if scene_sha_after != scene_sha_before or scene_bytes_after != scene_bytes:
        raise ScreenError("source scene changed across the host screen")
    _canonical_json(final)
    _write_new_json(args.output_root / FINAL_REPORT_NAME, final)
    print(
        json.dumps(
            {
                "decision": decision,
                "kind": KIND,
                "ok": True,
                "report": str(args.output_root / FINAL_REPORT_NAME),
            },
            sort_keys=True,
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(_cli_argv(argv))
    try:
        if args.child:
            return _child_main(args)
        return _host_main(args)
    except BaseException as exc:
        print(
            json.dumps(
                {
                    "error": f"{type(exc).__name__}: {exc}"[:4000],
                    "kind": CHILD_KIND if args.child else KIND,
                    "ok": False,
                    "schema_version": SCHEMA_VERSION,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
