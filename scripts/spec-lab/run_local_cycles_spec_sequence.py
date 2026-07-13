#!/usr/bin/env python3
"""Render a bounded contiguous frame sequence through the pinned local cx-agent.

This is a Stage-1 preview wrapper, not a production render or benchmark.  It
submits one multi-unit request so the Cycles backend can retain one Blender
scene/device session across the sequence, then independently checks every
selected manifest and PNG.  It never contacts control, bills work, attests an
artifact, or publishes a speed claim.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from typing import Any

from PIL import Image, UnidentifiedImageError


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
DEFAULT_AGENT = REPO_ROOT / "agent" / "target" / "debug" / "cx-agent"
DEFAULT_DRIVER = HERE / "cx_agent_render_preview_driver.py"
DEFAULT_BACKEND = HERE / "cx_cycles_render_preview_backend.py"
DEFAULT_CORE = HERE / "cx_speculative_core.py"
DEFAULT_ADAPTER = HERE / "cx_render_spec_adapter.py"
DEFAULT_BLENDER = Path("/Applications/Blender.app/Contents/MacOS/Blender")

REQUEST_KIND = "cx_spec_render_preview_request"
RESULT_KIND = "cx_spec_render_preview_result"
ARTIFACT_KIND = "cx_cycles_preview_artifact"
ARTIFACT_MANIFEST_KIND = "cx_cycles_preview_manifest"
SEQUENCE_MANIFEST_KIND = "cx_cycles_preview_sequence_manifest"
ARTIFACT_SCHEMA_VERSION = 2
SEQUENCE_SCHEMA_VERSION = 2
RECEIPT_TRUST = "local_experiment_unattested"
PNG_COMPRESSION = 0
CANDIDATE_PROFILE_NATIVE_SCOPE = "native_only"
NATIVE_CANDIDATE_PROFILE = {
    "name": "native",
    "max_bounces": None,
    "diffuse_bounces": None,
    "glossy_bounces": None,
    "transmission_bounces": None,
    "use_light_tree": None,
    "use_adaptive_sampling": False,
    "adaptive_min_samples": None,
    "adaptive_threshold": None,
}

MAX_FRAMES = 4_096
MAX_FRAME = 1_000_000
MAX_PIXELS = 4_194_304
MAX_AGENT_STDOUT = 32 << 20
MAX_AGENT_STDERR = 1 << 20
MAX_MANIFEST_BYTES = 256 << 10
MAX_VIDEO_BYTES = 8 << 30
MAX_JSON_DEPTH = 32

ENVELOPE_KEYS = {
    "schema_version",
    "kind",
    "preview_only",
    "billing_eligible",
    "production_ready",
    "receipt_trust",
    "outputs",
    "receipt",
}
RECEIPT_KEYS = {
    "schema_version",
    "draft_cost_s",
    "verify_cost_s",
    "accepted_fraction",
    "repair_cost_s",
    "overhead_cost_s",
    "total_product_time_s",
    "quality_tier",
    "speedup_vs_baseline",
    "exact",
    "modality",
    "branch_id",
    "units",
    "accepted_units",
    "repaired_units",
    "repaired_fraction",
    "baseline_total_time_s",
    "baseline_source",
    "quality_gate",
    "artifact_verified",
    "evidence",
    "global_ssim",
    "worst_tile_ssim",
    "p5_ssim",
    "claim_scope",
    "details",
}
ARTIFACT_MANIFEST_KEYS = {
    "schema_version",
    "kind",
    "preview_only",
    "billing_eligible",
    "production_ready",
    "artifact_verified",
    "evidence",
    "execution_identity_revalidation",
    "unit_id",
    "binding_sha256",
    "phase",
    "scene",
    "render",
    "artifact",
    "pins",
}
RENDERER_IDENTITY_KEYS = {
    "blender_build_hash",
    "blender_version",
    "candidate_profile",
    "dependency_count",
    "dependency_paths_sha256",
    "device",
    "enabled_device_names",
    "native_integrator",
    "png_compression",
    "reference_sampling",
}


class SequenceError(ValueError):
    """A closed request/result, content-pin, artifact, or mux violation."""


@dataclass(frozen=True)
class FilePin:
    path: Path
    sha256: str
    identity: tuple[int, int, int, int, int, int]


def sha256_file(path: Path, *, max_bytes: int | None = None) -> str:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise SequenceError(f"file exceeds its {max_bytes}-byte limit: {path}")
            digest.update(chunk)
    return digest.hexdigest()


def _file_identity(path: Path) -> tuple[int, int, int, int, int, int]:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise SequenceError(f"expected a regular file: {path}")
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _sha256_arg(raw: str) -> str:
    if not _is_sha256(raw):
        raise argparse.ArgumentTypeError(
            "SHA-256 pins must be 64 lowercase hexadecimal characters"
        )
    return raw


def _positive_int(raw: str) -> int:
    value = int(raw, 10)
    if value <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", type=Path, default=DEFAULT_AGENT)
    parser.add_argument(
        "--agent-sha256",
        required=True,
        type=_sha256_arg,
        help="operator-expected SHA-256 of the compiled cx-agent",
    )
    parser.add_argument("--driver", type=Path, default=DEFAULT_DRIVER)
    parser.add_argument("--backend", type=Path, default=DEFAULT_BACKEND)
    parser.add_argument("--core", type=Path, default=DEFAULT_CORE)
    parser.add_argument("--adapter", type=Path, default=DEFAULT_ADAPTER)
    parser.add_argument("--blender", type=Path, default=DEFAULT_BLENDER)
    parser.add_argument("--scene", required=True, type=Path)
    parser.add_argument(
        "--scene-root",
        type=Path,
        help="bundle root copied by the backend (default: scene parent)",
    )
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--device", choices=("CPU", "METAL"), default="METAL")
    parser.add_argument("--width", type=_positive_int, default=1920)
    parser.add_argument("--height", type=_positive_int, default=1080)
    parser.add_argument("--frame-start", type=int, required=True)
    parser.add_argument("--frame-end", type=int, required=True)
    parser.add_argument("--draft-samples", type=_positive_int, default=16)
    parser.add_argument("--verify-samples", type=_positive_int, default=16)
    parser.add_argument("--repair-samples", type=_positive_int, default=4096)
    parser.add_argument("--timeout-secs", type=_positive_int, default=3600)
    parser.add_argument("--fps", type=_positive_int, default=24)
    parser.add_argument(
        "--video-out",
        type=Path,
        help="optional new .mp4 path for a silent fixed-config H.264 mux",
    )
    parser.add_argument(
        "--ffmpeg",
        type=Path,
        help="absolute ffmpeg executable; required with --video-out",
    )
    parser.add_argument(
        "--ffmpeg-sha256",
        type=_sha256_arg,
        help="operator-expected ffmpeg SHA-256; required with --video-out",
    )
    parser.add_argument(
        "--ffprobe",
        type=Path,
        help="absolute ffprobe executable; required with --video-out",
    )
    parser.add_argument(
        "--ffprobe-sha256",
        type=_sha256_arg,
        help="operator-expected ffprobe SHA-256; required with --video-out",
    )
    return parser.parse_args(argv)


def _canonical_path(path: Path, name: str, *, directory: bool = False) -> Path:
    if not path.is_absolute():
        raise SequenceError(f"{name} must be absolute")
    try:
        canonical = path.resolve(strict=True)
    except OSError as exc:
        raise SequenceError(f"cannot resolve {name}={path}: {exc}") from exc
    if directory and not canonical.is_dir():
        raise SequenceError(f"{name} must name a directory")
    if not directory and not canonical.is_file():
        raise SequenceError(f"{name} must name a regular file")
    return canonical


def validate_args(args: argparse.Namespace) -> None:
    for name in ("agent", "driver", "backend", "core", "adapter", "blender", "scene"):
        if not getattr(args, name).is_absolute():
            raise SequenceError(f"--{name.replace('_', '-')} must be absolute")
    if not args.output_root.is_absolute():
        raise SequenceError("--output-root must be absolute")
    if args.scene_root is not None and not args.scene_root.is_absolute():
        raise SequenceError("--scene-root must be absolute")
    if args.scene.suffix != ".blend":
        raise SequenceError("--scene must end in lowercase .blend")
    if not 16 <= args.width <= 4096 or not 16 <= args.height <= 4096:
        raise SequenceError("width and height must each be in [16,4096]")
    if args.width * args.height > MAX_PIXELS:
        raise SequenceError(f"width*height exceeds {MAX_PIXELS}")
    if not 0 <= args.frame_start <= args.frame_end <= MAX_FRAME:
        raise SequenceError(f"frames must satisfy 0 <= start <= end <= {MAX_FRAME}")
    if args.frame_end - args.frame_start + 1 > MAX_FRAMES:
        raise SequenceError(f"sequence exceeds the {MAX_FRAMES}-frame limit")
    if not 1 <= args.draft_samples <= 64 or not 1 <= args.verify_samples <= 64:
        raise SequenceError("draft/verify samples must each be in [1,64]")
    if not 2 <= args.repair_samples <= 4096 or args.repair_samples <= max(
        args.draft_samples, args.verify_samples
    ):
        raise SequenceError(
            "repair samples must be in [2,4096] and exceed draft/verify samples"
        )
    if not 1 <= args.timeout_secs <= 3600:
        raise SequenceError("timeout must be in [1,3600] seconds")
    if not 1 <= args.fps <= 120:
        raise SequenceError("fps must be in [1,120]")
    video_fields = (
        args.video_out is not None,
        args.ffmpeg is not None,
        args.ffmpeg_sha256 is not None,
        args.ffprobe is not None,
        args.ffprobe_sha256 is not None,
    )
    if any(video_fields) and not all(video_fields):
        raise SequenceError(
            "--video-out, --ffmpeg/--ffmpeg-sha256, and "
            "--ffprobe/--ffprobe-sha256 must be supplied together"
        )
    if args.video_out is not None:
        if not args.video_out.is_absolute() or args.video_out.suffix != ".mp4":
            raise SequenceError("--video-out must be an absolute lowercase .mp4 path")
        if args.width % 2 or args.height % 2:
            raise SequenceError("silent H.264 output requires even width and height")
        if not args.ffmpeg.is_absolute():
            raise SequenceError("--ffmpeg must be absolute")
        if not args.ffprobe.is_absolute():
            raise SequenceError("--ffprobe must be absolute")


def _pin_file(
    path: Path,
    name: str,
    *,
    expected_sha256: str | None = None,
    executable: bool = False,
) -> FilePin:
    canonical = _canonical_path(path, name)
    if executable and not os.access(canonical, os.X_OK):
        raise SequenceError(f"{name} is not executable: {canonical}")
    identity = _file_identity(canonical)
    actual = sha256_file(canonical)
    if expected_sha256 is not None and actual != expected_sha256:
        raise SequenceError(
            f"{name} SHA-256 mismatch: expected {expected_sha256}, got {actual}"
        )
    if _file_identity(canonical) != identity:
        raise SequenceError(f"{name} changed while it was pinned")
    return FilePin(canonical, actual, identity)


def _revalidate_pin(pin: FilePin, name: str) -> None:
    if _file_identity(pin.path) != pin.identity or sha256_file(pin.path) != pin.sha256:
        raise SequenceError(f"pinned {name} changed during sequence execution")


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise SequenceError("value is not finite canonical JSON") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SequenceError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise SequenceError(f"non-finite JSON constant {value!r}")


def _json_depth(value: Any) -> int:
    maximum = 0
    stack = [(value, 0)]
    while stack:
        current, parent = stack.pop()
        if isinstance(current, dict):
            depth = parent + 1
            maximum = max(maximum, depth)
            stack.extend((child, depth) for child in current.values())
        elif isinstance(current, list):
            depth = parent + 1
            maximum = max(maximum, depth)
            stack.extend((child, depth) for child in current)
    return maximum


def _strict_json(raw: bytes, name: str) -> Any:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except SequenceError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise SequenceError(f"{name} is not strict UTF-8 JSON: {exc}") from exc
    if _json_depth(value) > MAX_JSON_DEPTH:
        raise SequenceError(f"{name} exceeds the {MAX_JSON_DEPTH}-level nesting limit")
    return value


def _write_new_file(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)


def _read_regular(path: Path, *, max_bytes: int, name: str) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SequenceError(f"cannot open {name} {path}: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= max_bytes:
            raise SequenceError(f"{name} must be a nonempty regular file <= {max_bytes} bytes")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(fd, min(1 << 20, max_bytes + 1 - total)):
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise SequenceError(f"{name} exceeds {max_bytes} bytes")
        after = os.fstat(fd)
        if _identity_from_stat(before) != _identity_from_stat(after):
            raise SequenceError(f"{name} changed while it was read")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _identity_from_stat(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _strict_relative(value: Any, name: str) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 4096
        or "\\" in value
        or "\x00" in value
    ):
        raise SequenceError(f"{name} must be a strict relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise SequenceError(f"{name} must be a strict relative POSIX path")
    return path


def _resolve_output_file(root: Path, value: Any, name: str) -> Path:
    relative = _strict_relative(value, name)
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        try:
            info = cursor.lstat()
        except OSError as exc:
            raise SequenceError(f"cannot resolve {name} component {cursor}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise SequenceError(f"{name} may not traverse a symlink")
    try:
        candidate = cursor.resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, ValueError) as exc:
        raise SequenceError(f"{name} escaped the output root") from exc
    if not candidate.is_file():
        raise SequenceError(f"{name} does not name a regular file")
    return candidate


def _kill_process_tree(process: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    try:
        process.kill()
    except OSError:
        pass


def _run_command(
    command: list[str],
    *,
    environment: dict[str, str],
    timeout: int,
    stdout_limit: int,
    stderr_limit: int,
    label: str,
) -> tuple[bytes, bytes, float]:
    started = time.perf_counter()
    try:
        process = subprocess.Popen(  # noqa: S603 - every executable is content-pinned
            command,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        raise SequenceError(f"could not launch {label}: {exc}") from exc
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_process_tree(process)
        process.wait()
        raise SequenceError(f"{label} exceeded its {timeout}s wrapper timeout") from exc
    if len(stdout) > stdout_limit or len(stderr) > stderr_limit:
        raise SequenceError(
            f"{label} exceeded bounded output ({stdout_limit} stdout, {stderr_limit} stderr bytes)"
        )
    if process.returncode != 0:
        tail = stderr[-4096:].decode("utf-8", errors="replace").strip()
        raise SequenceError(f"{label} exited {process.returncode}: {tail}")
    return stdout, stderr, time.perf_counter() - started


def _validate_receipt(receipt: Any, units: int) -> None:
    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS:
        raise SequenceError("agent receipt has an unknown or incomplete shape")
    if (
        receipt["schema_version"] != 1
        or receipt["modality"] != "render"
        or receipt["branch_id"] != "agent-render-preview-v1"
        or not isinstance(receipt["units"], int)
        or isinstance(receipt["units"], bool)
        or receipt["units"] != units
        or not isinstance(receipt["accepted_units"], int)
        or isinstance(receipt["accepted_units"], bool)
        or not isinstance(receipt["repaired_units"], int)
        or isinstance(receipt["repaired_units"], bool)
        or not 0 <= receipt["accepted_units"] <= units
        or not 0 <= receipt["repaired_units"] <= units
        or receipt["accepted_units"] + receipt["repaired_units"] != units
        or receipt["exact"] is not False
        or receipt["artifact_verified"] is not False
        or receipt["evidence"] != "synthetic"
        or receipt["baseline_source"] != "absent"
        or receipt["baseline_total_time_s"] != 0.0
        or receipt["speedup_vs_baseline"] is not None
        or receipt["global_ssim"] is not None
        or receipt["worst_tile_ssim"] is not None
        or receipt["p5_ssim"] is not None
        or receipt["quality_gate"] is not True
        or receipt["quality_tier"] != "preview"
        or not isinstance(receipt["details"], dict)
    ):
        raise SequenceError("agent receipt escaped the local synthetic preview contract")
    finite_fields = (
        "draft_cost_s",
        "verify_cost_s",
        "repair_cost_s",
        "overhead_cost_s",
        "total_product_time_s",
        "accepted_fraction",
        "repaired_fraction",
    )
    if any(
        not isinstance(receipt[key], (int, float))
        or isinstance(receipt[key], bool)
        or not math.isfinite(receipt[key])
        or receipt[key] < 0
        for key in finite_fields
    ):
        raise SequenceError("agent receipt contains invalid numeric values")
    phase_sum = (
        receipt["draft_cost_s"]
        + receipt["verify_cost_s"]
        + receipt["repair_cost_s"]
        + receipt["overhead_cost_s"]
    )
    expected_accepted = receipt["accepted_units"] / units
    expected_repaired = receipt["repaired_units"] / units
    if (
        receipt["total_product_time_s"] <= 0
        or abs(receipt["total_product_time_s"] - phase_sum) > 5e-5
        or not 0 <= receipt["accepted_fraction"] <= 1
        or not 0 <= receipt["repaired_fraction"] <= 1
        or abs(receipt["accepted_fraction"] - expected_accepted) > 1e-6
        or abs(receipt["repaired_fraction"] - expected_repaired) > 1e-6
    ):
        raise SequenceError("agent receipt counts, fractions, or phase total contradict")


def _validate_envelope(value: Any, frame_count: int) -> tuple[list[Any], dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != ENVELOPE_KEYS:
        raise SequenceError("agent result has an unknown or incomplete envelope shape")
    if (
        value["schema_version"] != 1
        or value["kind"] != RESULT_KIND
        or value["preview_only"] is not True
        or value["billing_eligible"] is not False
        or value["production_ready"] is not False
        or value["receipt_trust"] != RECEIPT_TRUST
        or not isinstance(value["outputs"], list)
        or len(value["outputs"]) != frame_count
    ):
        raise SequenceError("agent result escaped the bounded local preview contract")
    _validate_receipt(value["receipt"], frame_count)
    return value["outputs"], value["receipt"]


def _expected_binding(unit: dict[str, Any]) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "binding_policy": "render-preview-operator-policy-v2",
                "modality": "render",
                "operator_policy": {
                    "candidate_profile": NATIVE_CANDIDATE_PROFILE,
                    "candidate_profile_scope": CANDIDATE_PROFILE_NATIVE_SCOPE,
                    "profile_authorization": "native_only",
                    "png_compression": PNG_COMPRESSION,
                },
                "payload": unit["payload"],
                "unit_id": unit["unit_id"],
            }
        )
    ).hexdigest()


def _validate_renderer_identity(value: Any, expected_device: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != RENDERER_IDENTITY_KEYS:
        raise SequenceError("artifact manifest lacks a closed renderer identity")
    enabled = value["enabled_device_names"]
    native_integrator = value["native_integrator"]
    reference_sampling = value["reference_sampling"]
    if (
        value["device"] != expected_device
        or not isinstance(value["blender_version"], str)
        or not value["blender_version"]
        or len(value["blender_version"]) > 128
        or not isinstance(value["blender_build_hash"], str)
        or not value["blender_build_hash"]
        or len(value["blender_build_hash"]) > 128
        or not isinstance(value["dependency_count"], int)
        or isinstance(value["dependency_count"], bool)
        or not 0 <= value["dependency_count"] <= 4096
        or not _is_sha256(value["dependency_paths_sha256"])
        or not isinstance(enabled, list)
        or not 1 <= len(enabled) <= 16
        or any(not isinstance(name, str) or not name or len(name) > 256 for name in enabled)
        or value["candidate_profile"] != NATIVE_CANDIDATE_PROFILE
        or value["png_compression"] != PNG_COMPRESSION
        or not isinstance(native_integrator, dict)
        or set(native_integrator)
        != {
            "max_bounces",
            "diffuse_bounces",
            "glossy_bounces",
            "transmission_bounces",
        }
        or any(
            not isinstance(setting, int)
            or isinstance(setting, bool)
            or not 0 <= setting <= 128
            for setting in native_integrator.values()
        )
        or not isinstance(reference_sampling, dict)
        or set(reference_sampling)
        != {
            "use_light_tree",
            "use_adaptive_sampling",
            "adaptive_min_samples",
            "adaptive_threshold",
        }
        or type(reference_sampling.get("use_light_tree")) is not bool
        or reference_sampling.get("use_adaptive_sampling") is not False
        or not isinstance(reference_sampling.get("adaptive_min_samples"), int)
        or isinstance(reference_sampling.get("adaptive_min_samples"), bool)
        or not 0 <= reference_sampling["adaptive_min_samples"] <= 4096
        or isinstance(reference_sampling.get("adaptive_threshold"), bool)
        or not isinstance(reference_sampling.get("adaptive_threshold"), (int, float))
        or not math.isfinite(float(reference_sampling["adaptive_threshold"]))
        or not 0.0 <= float(reference_sampling["adaptive_threshold"]) <= 1.0
    ):
        raise SequenceError("artifact renderer identity is malformed or uses the wrong device")
    return value


def _validate_integrator_policy(
    value: Any, renderer: dict[str, Any], index: int
) -> None:
    expected_keys = {
        "mode",
        "candidate_profile",
        "candidate_profile_scope",
        "actual_integrator",
        "actual_sampling",
        "samples_are_cap_when_adaptive",
        "repair_and_baseline_use_reference_policy",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected_keys
        or value["mode"] != "fixed_reference"
        or value["candidate_profile"] != NATIVE_CANDIDATE_PROFILE
        or value["candidate_profile_scope"] != CANDIDATE_PROFILE_NATIVE_SCOPE
        or value["actual_integrator"] != renderer["native_integrator"]
        or value["actual_sampling"] != renderer["reference_sampling"]
        or value["samples_are_cap_when_adaptive"] is not False
        or value["repair_and_baseline_use_reference_policy"] is not True
    ):
        raise SequenceError(
            f"artifact manifest {index} integrator policy escaped scene-native mode"
        )


def _validate_png(path: Path, expected_sha: str, width: int, height: int) -> tuple[str, int]:
    if not _is_sha256(expected_sha):
        raise SequenceError("artifact manifest PNG digest is malformed")
    before = _file_identity(path)
    actual = sha256_file(path, max_bytes=min(64 << 20, width * height * 8 + (1 << 20)))
    if actual != expected_sha:
        raise SequenceError(f"PNG SHA-256 mismatch for {path}")
    try:
        with Image.open(path) as image:
            if image.format != "PNG" or image.size != (width, height):
                raise SequenceError(
                    f"PNG shape/format mismatch for {path}: {image.format} {image.size}"
                )
            image.load()
    except (OSError, UnidentifiedImageError) as exc:
        raise SequenceError(f"cannot decode PNG {path}: {exc}") from exc
    if _file_identity(path) != before or sha256_file(path) != actual:
        raise SequenceError(f"PNG changed while it was validated: {path}")
    return actual, before[3]


def _validate_selected_outputs(
    outputs: list[Any],
    units: list[dict[str, Any]],
    *,
    output_root: Path,
    scene_relative: str,
    scene_sha256: str,
    args: argparse.Namespace,
    pins: dict[str, FilePin],
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    common_renderer: dict[str, Any] | None = None
    common_bundle: dict[str, Any] | None = None
    seen_manifests: set[Path] = set()
    seen_artifacts: set[Path] = set()
    expected_device = "GPU/METAL" if args.device == "METAL" else "CPU"

    for index, (descriptor, unit) in enumerate(zip(outputs, units, strict=True)):
        if (
            not isinstance(descriptor, dict)
            or set(descriptor) != {"schema_version", "kind", "manifest_path"}
            or descriptor["schema_version"] != ARTIFACT_SCHEMA_VERSION
            or descriptor["kind"] != ARTIFACT_KIND
        ):
            raise SequenceError(f"output {index} is not a closed artifact descriptor")
        manifest_path = _resolve_output_file(
            output_root, descriptor["manifest_path"], f"output {index} manifest_path"
        )
        if manifest_path in seen_manifests:
            raise SequenceError("sequence reused an artifact manifest")
        seen_manifests.add(manifest_path)
        manifest_raw = _read_regular(
            manifest_path, max_bytes=MAX_MANIFEST_BYTES, name="artifact manifest"
        )
        manifest = _strict_json(manifest_raw, "artifact manifest")
        expected_keys = ARTIFACT_MANIFEST_KEYS | (
            {"repair_strategy"} if isinstance(manifest, dict) and manifest.get("phase") == "repair" else set()
        )
        if not isinstance(manifest, dict) or set(manifest) != expected_keys:
            raise SequenceError("artifact manifest has an unknown or incomplete shape")
        phase = manifest["phase"]
        payload = unit["payload"]
        expected_samples = (
            args.draft_samples if phase == "draft" else args.repair_samples
        )
        if (
            phase not in {"draft", "repair"}
            or manifest["schema_version"] != ARTIFACT_SCHEMA_VERSION
            or manifest["kind"] != ARTIFACT_MANIFEST_KIND
            or manifest["preview_only"] is not True
            or manifest["billing_eligible"] is not False
            or manifest["production_ready"] is not False
            or manifest["artifact_verified"] is not False
            or manifest["evidence"] != "synthetic"
            or manifest["unit_id"] != unit["unit_id"]
            or manifest["binding_sha256"] != _expected_binding(unit)
            or manifest["execution_identity_revalidation"]
            != {
                "initial_content": "sha256",
                "per_render": "pre_and_post_stat_identity_plus_bundle_file_set",
            }
        ):
            raise SequenceError(f"artifact manifest {index} identity/contract mismatch")

        scene = manifest["scene"]
        if (
            not isinstance(scene, dict)
            or set(scene) != {"relative_path", "sha256", "bundle_sha256", "bundle_files", "bundle_bytes"}
            or scene["relative_path"] != scene_relative
            or scene["sha256"] != scene_sha256
            or not _is_sha256(scene["bundle_sha256"])
            or not isinstance(scene["bundle_files"], int)
            or isinstance(scene["bundle_files"], bool)
            or not 1 <= scene["bundle_files"] <= 4096
            or not isinstance(scene["bundle_bytes"], int)
            or isinstance(scene["bundle_bytes"], bool)
            or scene["bundle_bytes"] <= 0
        ):
            raise SequenceError(f"artifact manifest {index} scene/bundle identity mismatch")
        bundle = {
            "sha256": scene["bundle_sha256"],
            "files": scene["bundle_files"],
            "bytes": scene["bundle_bytes"],
        }
        if common_bundle is None:
            common_bundle = bundle
        elif bundle != common_bundle:
            raise SequenceError("sequence frames do not share one scene-bundle identity")

        render = manifest["render"]
        if not isinstance(render, dict) or set(render) != {
            "engine",
            "device",
            "width",
            "height",
            "frame",
            "samples",
            "sample_offset",
            "seed",
            "integrator_policy",
            "pixel_filter",
            "png_compression",
            "worker_renderer_identity",
        }:
            raise SequenceError(f"artifact manifest {index} render shape mismatch")
        if (
            render["engine"] != "CYCLES"
            or render["device"] != expected_device
            or render["width"] != args.width
            or render["height"] != args.height
            or render["frame"] != payload["frame"]
            or render["samples"] != expected_samples
            or render["sample_offset"] != 0
            or not isinstance(render["seed"], int)
            or isinstance(render["seed"], bool)
            or render["pixel_filter"]
            != {"type": "BLACKMAN_HARRIS", "width": 1.5}
            or render["png_compression"] != PNG_COMPRESSION
        ):
            raise SequenceError(f"artifact manifest {index} render config/order mismatch")
        renderer = _validate_renderer_identity(
            render["worker_renderer_identity"], expected_device
        )
        _validate_integrator_policy(render["integrator_policy"], renderer, index)
        if common_renderer is None:
            common_renderer = renderer
        elif renderer != common_renderer:
            raise SequenceError("sequence frames do not share one renderer identity")

        artifact = manifest["artifact"]
        if (
            not isinstance(artifact, dict)
            or set(artifact) != {"path", "sha256", "media_type"}
            or artifact["media_type"] != "image/png"
        ):
            raise SequenceError(f"artifact manifest {index} artifact shape mismatch")
        artifact_path = _resolve_output_file(
            output_root, artifact["path"], f"output {index} artifact path"
        )
        if artifact_path in seen_artifacts:
            raise SequenceError("sequence reused a PNG artifact")
        seen_artifacts.add(artifact_path)
        artifact_sha, artifact_bytes = _validate_png(
            artifact_path, artifact["sha256"], args.width, args.height
        )

        pin_rows = manifest["pins"]
        if not isinstance(pin_rows, dict) or set(pin_rows) != {
            "blender_sha256",
            "backend_sha256",
            "child_script_sha256",
            "controller_core_sha256",
            "controller_adapter_sha256",
        }:
            raise SequenceError(f"artifact manifest {index} pin shape mismatch")
        if (
            pin_rows["blender_sha256"] != pins["blender"].sha256
            or pin_rows["backend_sha256"] != pins["backend"].sha256
            or pin_rows["controller_core_sha256"] != pins["core"].sha256
            or pin_rows["controller_adapter_sha256"] != pins["adapter"].sha256
            or not _is_sha256(pin_rows["child_script_sha256"])
        ):
            raise SequenceError(f"artifact manifest {index} content pin mismatch")

        frames.append(
            {
                "index": index,
                "frame": payload["frame"],
                "unit_id": unit["unit_id"],
                "phase": phase,
                "manifest_path": descriptor["manifest_path"],
                "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
                "artifact_path": artifact["path"],
                "artifact_sha256": artifact_sha,
                "artifact_bytes": artifact_bytes,
                "width": args.width,
                "height": args.height,
                "_absolute_artifact_path": artifact_path,
                "_absolute_manifest_path": manifest_path,
            }
        )
    assert common_renderer is not None and common_bundle is not None
    return frames, common_renderer, common_bundle


def _revalidate_frame_sources(frames: list[dict[str, Any]]) -> None:
    for row in frames:
        manifest_raw = _read_regular(
            row["_absolute_manifest_path"],
            max_bytes=MAX_MANIFEST_BYTES,
            name="artifact manifest",
        )
        if hashlib.sha256(manifest_raw).hexdigest() != row["manifest_sha256"]:
            raise SequenceError("artifact manifest changed after sequence validation")
        _validate_png(
            row["_absolute_artifact_path"],
            row["artifact_sha256"],
            row["width"],
            row["height"],
        )


def _snapshot_file(source: Path, destination: Path, expected_sha: str) -> None:
    source_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        source_flags |= os.O_NOFOLLOW
    source_fd = os.open(source, source_flags)
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        destination_flags |= os.O_NOFOLLOW
    destination_fd = os.open(destination, destination_flags, 0o600)
    digest = hashlib.sha256()
    try:
        before = os.fstat(source_fd)
        while chunk := os.read(source_fd, 1 << 20):
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                view = view[written:]
        os.fsync(destination_fd)
        after = os.fstat(source_fd)
        if _identity_from_stat(before) != _identity_from_stat(after):
            raise SequenceError("PNG changed while it was snapshotted for ffmpeg")
    finally:
        os.close(source_fd)
        os.close(destination_fd)
    if digest.hexdigest() != expected_sha or sha256_file(destination) != expected_sha:
        raise SequenceError("ffmpeg input snapshot digest mismatch")


def _parse_ffmpeg_progress(raw: bytes, expected_frames: int, fps: int) -> tuple[int, float]:
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise SequenceError("ffmpeg validation progress was not UTF-8") from exc
    rows: dict[str, str] = {}
    ended = False
    for line in lines:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        rows[key] = value
        ended = ended or (key == "progress" and value == "end")
    try:
        frames = int(rows["frame"], 10)
        duration = int(rows["out_time_us"], 10) / 1_000_000.0
    except (KeyError, ValueError) as exc:
        raise SequenceError("ffmpeg validation omitted frame/duration progress") from exc
    expected_duration = expected_frames / fps
    tolerance = max(0.002, 1.0 / fps)
    if (
        not ended
        or frames != expected_frames
        or not math.isfinite(duration)
        or duration <= 0
        or abs(duration - expected_duration) > tolerance
    ):
        raise SequenceError(
            "ffmpeg output frame count/duration mismatch: "
            f"frames={frames}, duration={duration}, expected={expected_frames}/{expected_duration}"
        )
    return frames, duration


def _fraction_equals_fps(value: Any, fps: int) -> bool:
    if not isinstance(value, str) or "/" not in value:
        return False
    numerator_raw, denominator_raw = value.split("/", 1)
    try:
        numerator = int(numerator_raw, 10)
        denominator = int(denominator_raw, 10)
    except ValueError:
        return False
    return denominator > 0 and numerator == fps * denominator


def _validate_ffprobe(
    raw: bytes, *, expected_frames: int, fps: int, width: int, height: int
) -> tuple[int, float]:
    probe = _strict_json(raw, "ffprobe result")
    if (
        not isinstance(probe, dict)
        or set(probe) not in (
            {"streams", "format"},
            {"programs", "stream_groups", "streams", "format"},
        )
        or ("programs" in probe and probe["programs"] != [])
        or ("stream_groups" in probe and probe["stream_groups"] != [])
    ):
        raise SequenceError("ffprobe returned an unknown or incomplete result shape")
    streams = probe["streams"]
    if not isinstance(streams, list) or len(streams) != 1:
        raise SequenceError("video must contain exactly one stream and no audio")
    stream = streams[0]
    if not isinstance(stream, dict):
        raise SequenceError("ffprobe stream row is malformed")
    required = {
        "index",
        "codec_name",
        "codec_type",
        "width",
        "height",
        "pix_fmt",
        "r_frame_rate",
        "avg_frame_rate",
        "nb_read_frames",
    }
    if not required.issubset(stream) or set(stream) - (required | {"duration"}):
        raise SequenceError("ffprobe stream identity has an unknown or incomplete shape")
    if (
        stream["index"] != 0
        or stream["codec_name"] != "h264"
        or stream["codec_type"] != "video"
        or stream["width"] != width
        or stream["height"] != height
        or stream["pix_fmt"] != "yuv420p"
        or not _fraction_equals_fps(stream["r_frame_rate"], fps)
        or not _fraction_equals_fps(stream["avg_frame_rate"], fps)
    ):
        raise SequenceError("video codec/pixel-format/framerate contract mismatch")
    try:
        frames = int(stream["nb_read_frames"], 10)
        format_row = probe["format"]
        if not isinstance(format_row, dict) or set(format_row) != {"duration"}:
            raise ValueError("format shape")
        duration = float(format_row["duration"])
    except (TypeError, ValueError, KeyError) as exc:
        raise SequenceError("ffprobe omitted a usable frame count/duration") from exc
    expected_duration = expected_frames / fps
    tolerance = max(0.002, 1.0 / fps)
    if (
        frames != expected_frames
        or not math.isfinite(duration)
        or duration <= 0
        or abs(duration - expected_duration) > tolerance
    ):
        raise SequenceError(
            "ffprobe frame count/duration mismatch: "
            f"frames={frames}, duration={duration}, "
            f"expected={expected_frames}/{expected_duration}"
        )
    return frames, duration


def _mux_video(
    frames: list[dict[str, Any]],
    *,
    args: argparse.Namespace,
    output_root: Path,
    ffmpeg_pin: FilePin,
    ffprobe_pin: FilePin,
) -> dict[str, Any]:
    mux_started = time.perf_counter()
    video_path = args.video_out
    assert video_path is not None
    parent = _canonical_path(video_path.parent, "video output parent", directory=True)
    video_path = parent / video_path.name
    if video_path.exists() or video_path.is_symlink():
        raise FileExistsError(f"refusing to overwrite video output: {video_path}")
    temporary_video = parent / (
        f".{video_path.stem}.{secrets.token_hex(16)}.tmp{video_path.suffix}"
    )
    stage = Path(tempfile.mkdtemp(prefix="sequence-mux-", dir=output_root))
    os.chmod(stage, 0o700)
    try:
        for index, frame in enumerate(frames):
            destination = stage / f"frame-{index:08d}.png"
            _snapshot_file(
                frame["_absolute_artifact_path"], destination, frame["artifact_sha256"]
            )
        config = {
            "schema_version": 1,
            "container": "mp4",
            "codec": "h264_videotoolbox",
            "allow_software_fallback": False,
            "target_bitrate": "12M",
            "pixel_format": "yuv420p",
            "audio": "none",
            "framerate": args.fps,
            "frame_count": len(frames),
            "input_pattern": "frame-%08d.png",
            "start_number": 0,
            "movflags": "+faststart",
        }
        config_sha = hashlib.sha256(_canonical_json(config)).hexdigest()
        encode = [
            str(ffmpeg_pin.path),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-n",
            "-framerate",
            str(args.fps),
            "-start_number",
            "0",
            "-i",
            str(stage / "frame-%08d.png"),
            "-frames:v",
            str(len(frames)),
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "h264_videotoolbox",
            "-allow_sw",
            "0",
            "-b:v",
            "12M",
            "-pix_fmt",
            "yuv420p",
            "-fps_mode",
            "cfr",
            "-movflags",
            "+faststart",
            str(temporary_video),
        ]
        environment = {"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"}
        _revalidate_pin(ffmpeg_pin, "ffmpeg")
        _, _, encode_wall_s = _run_command(
            encode,
            environment=environment,
            timeout=args.timeout_secs,
            stdout_limit=1 << 20,
            stderr_limit=1 << 20,
            label="pinned ffmpeg encode",
        )
        _revalidate_pin(ffmpeg_pin, "ffmpeg")
        if temporary_video.is_symlink() or not temporary_video.is_file():
            raise SequenceError("ffmpeg did not create a non-symlink regular video")
        if not 1 <= temporary_video.stat().st_size <= MAX_VIDEO_BYTES:
            raise SequenceError("ffmpeg video size is outside the fixed bound")
        for index, frame in enumerate(frames):
            if sha256_file(stage / f"frame-{index:08d}.png") != frame["artifact_sha256"]:
                raise SequenceError("ffmpeg input snapshot changed during encoding")

        validate = [
            str(ffmpeg_pin.path),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            "-nostats",
            "-i",
            str(temporary_video),
            "-map",
            "0:v:0",
            "-an",
            "-f",
            "null",
            "-",
        ]
        progress, _, decode_wall_s = _run_command(
            validate,
            environment=environment,
            timeout=args.timeout_secs,
            stdout_limit=1 << 20,
            stderr_limit=1 << 20,
            label="pinned ffmpeg decode validation",
        )
        decoded_frames, observed_duration = _parse_ffmpeg_progress(
            progress, len(frames), args.fps
        )
        _revalidate_pin(ffmpeg_pin, "ffmpeg")

        probe_command = [
            str(ffprobe_pin.path),
            "-v",
            "error",
            "-count_frames",
            "-show_entries",
            (
                "stream=index,codec_name,codec_type,pix_fmt,r_frame_rate,"
                "avg_frame_rate,width,height,nb_read_frames,duration:format=duration"
            ),
            "-of",
            "json",
            str(temporary_video),
        ]
        _revalidate_pin(ffprobe_pin, "ffprobe")
        probe_raw, _, probe_wall_s = _run_command(
            probe_command,
            environment=environment,
            timeout=args.timeout_secs,
            stdout_limit=1 << 20,
            stderr_limit=1 << 20,
            label="pinned ffprobe stream validation",
        )
        probed_frames, probed_duration = _validate_ffprobe(
            probe_raw,
            expected_frames=len(frames),
            fps=args.fps,
            width=args.width,
            height=args.height,
        )
        if probed_frames != decoded_frames:
            raise SequenceError("ffmpeg decode and ffprobe frame counts disagree")
        _revalidate_pin(ffprobe_pin, "ffprobe")
        before = _file_identity(temporary_video)
        video_sha = sha256_file(temporary_video, max_bytes=MAX_VIDEO_BYTES)
        if _file_identity(temporary_video) != before:
            raise SequenceError("video changed while its digest was computed")
        result = {
            "path": str(video_path),
            "sha256": video_sha,
            "bytes": before[3],
            "media_type": "video/mp4",
            "silent": True,
            "frame_count": probed_frames,
            "fps": args.fps,
            "expected_duration_s": round(len(frames) / args.fps, 9),
            "observed_duration_s": round(probed_duration, 9),
            "decode_progress_duration_s": round(observed_duration, 9),
            "ffmpeg_path": str(ffmpeg_pin.path),
            "ffmpeg_sha256": ffmpeg_pin.sha256,
            "ffprobe_path": str(ffprobe_pin.path),
            "ffprobe_sha256": ffprobe_pin.sha256,
            "ffmpeg_config": config,
            "ffmpeg_config_sha256": config_sha,
            "timing_s": {
                "encode": round(encode_wall_s, 6),
                "decode_validation": round(decode_wall_s, 6),
                "ffprobe_validation": round(probe_wall_s, 6),
                "total_wall": None,
            },
        }
        # Publish only the fully decoded/probed/digested private temp. A hard
        # link is atomic and fails if a concurrent creator won the final name;
        # this code never unlinks that independently owned destination.
        os.link(temporary_video, video_path, follow_symlinks=False)
        parent_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        # link(2) legitimately updates inode ctime/link count; stable device,
        # inode, mode, size and content are the publication identity here.
        if (
            _file_identity(video_path)[:4] != before[:4]
            or sha256_file(video_path) != video_sha
        ):
            raise SequenceError("published video identity/digest mismatch")
        result["timing_s"]["total_wall"] = round(
            time.perf_counter() - mux_started, 6
        )
        return result
    finally:
        try:
            temporary_video.unlink()
        except FileNotFoundError:
            pass
        shutil.rmtree(stage, ignore_errors=True)


def _write_content_addressed_manifest(
    output_root: Path, manifest: dict[str, Any]
) -> tuple[Path, str]:
    output_root = output_root.resolve(strict=True)
    encoded = _canonical_json(manifest) + b"\n"
    digest = hashlib.sha256(encoded).hexdigest()
    directory = output_root / "sequences"
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        if directory.is_symlink() or not directory.is_dir():
            raise SequenceError("sequence manifest directory is not a real directory")
    directory = directory.resolve(strict=True)
    directory.relative_to(output_root)
    target = directory / f"sequence-{digest}.json"
    temporary = directory / f".{target.name}.{secrets.token_hex(16)}.tmp"
    _write_new_file(temporary, encoded)
    try:
        os.link(temporary, target, follow_symlinks=False)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    if sha256_file(target) != digest:
        raise SequenceError("content-addressed sequence manifest verification failed")
    return target, digest


def run_sequence(args: argparse.Namespace) -> dict[str, Any]:
    validate_args(args)
    output_root = _canonical_path(args.output_root, "output root", directory=True)
    scene = _canonical_path(args.scene, "scene")
    scene_root = _canonical_path(
        args.scene_root if args.scene_root is not None else scene.parent,
        "scene root",
        directory=True,
    )
    try:
        scene_relative_path = scene.relative_to(scene_root)
    except ValueError as exc:
        raise SequenceError("scene must be contained by --scene-root") from exc
    scene_relative = scene_relative_path.as_posix()
    _strict_relative(scene_relative, "scene relative path")
    if len(scene_relative.encode("utf-8")) > 1024:
        raise SequenceError("scene relative path exceeds the backend's 1024-byte limit")
    for left, right in ((output_root, scene_root), (scene_root, output_root)):
        try:
            left.relative_to(right)
        except ValueError:
            pass
        else:
            raise SequenceError("scene and output roots must not overlap")

    pins = {
        "sequence_wrapper": _pin_file(
            Path(__file__).resolve(), "sequence wrapper"
        ),
        "agent": _pin_file(
            args.agent, "cx-agent", expected_sha256=args.agent_sha256, executable=True
        ),
        "driver": _pin_file(args.driver, "render preview driver", executable=True),
        "backend": _pin_file(args.backend, "Cycles preview backend"),
        "core": _pin_file(args.core, "speculative controller core"),
        "adapter": _pin_file(args.adapter, "render spec adapter"),
        "blender": _pin_file(args.blender, "Blender", executable=True),
        "scene": _pin_file(scene, "scene"),
    }
    ffmpeg_pin = None
    ffprobe_pin = None
    if args.ffmpeg is not None:
        ffmpeg_pin = _pin_file(
            args.ffmpeg,
            "ffmpeg",
            expected_sha256=args.ffmpeg_sha256,
            executable=True,
        )
        ffprobe_pin = _pin_file(
            args.ffprobe,
            "ffprobe",
            expected_sha256=args.ffprobe_sha256,
            executable=True,
        )

    frames_raw = list(range(args.frame_start, args.frame_end + 1))
    config = {
        "schema_version": SEQUENCE_SCHEMA_VERSION,
        "scene_path": scene_relative,
        "scene_sha256": pins["scene"].sha256,
        "width": args.width,
        "height": args.height,
        "frame_start": args.frame_start,
        "frame_end": args.frame_end,
        "frame_count": len(frames_raw),
        "draft_samples": args.draft_samples,
        "verify_samples": args.verify_samples,
        "repair_samples": args.repair_samples,
        "device": args.device,
    }
    config_sha = hashlib.sha256(_canonical_json(config)).hexdigest()
    units: list[dict[str, Any]] = []
    for index, frame in enumerate(frames_raw):
        units.append(
            {
                "unit_id": f"sequence-frame-{frame:07d}",
                "payload": {
                    "scene_path": scene_relative,
                    "scene_sha256": pins["scene"].sha256,
                    "width": args.width,
                    "height": args.height,
                    "frame": frame,
                    "draft_samples": args.draft_samples,
                    "verify_samples": args.verify_samples,
                    "repair_samples": args.repair_samples,
                },
                "meta": {"sequence_index": index, "frame": frame},
            }
        )
    request = {
        "schema_version": 1,
        "kind": REQUEST_KIND,
        "units": units,
        "meta": {
            "preview_only": True,
            "billing_eligible": False,
            "production_ready": False,
            "receipt_trust": RECEIPT_TRUST,
            "sequence_config_sha256": config_sha,
        },
    }
    request_bytes = _canonical_json(request) + b"\n"
    if len(request_bytes) > 16 << 20:
        raise SequenceError("sequence request exceeds the 16 MiB agent limit")
    request_sha = hashlib.sha256(request_bytes).hexdigest()

    environment = os.environ.copy()
    environment.update(
        {
            "CX_SPEC_RENDER_PREVIEW_DRIVER": str(pins["driver"].path),
            "CX_SPEC_RENDER_PREVIEW_DRIVER_SHA256": pins["driver"].sha256,
            "CX_SPEC_RENDER_PREVIEW_BACKEND": str(pins["backend"].path),
            "CX_SPEC_RENDER_PREVIEW_BACKEND_SHA256": pins["backend"].sha256,
            "CX_SPEC_RENDER_PREVIEW_CORE_SHA256": pins["core"].sha256,
            "CX_SPEC_RENDER_PREVIEW_ADAPTER_SHA256": pins["adapter"].sha256,
            "CX_SPEC_RENDER_PREVIEW_TIMEOUT_SECS": str(args.timeout_secs),
            "CX_SPEC_RENDER_CYCLES_BLENDER": str(pins["blender"].path),
            "CX_SPEC_RENDER_CYCLES_BLENDER_SHA256": pins["blender"].sha256,
            "CX_SPEC_RENDER_CYCLES_SCENE_ROOT": str(scene_root),
            "CX_SPEC_RENDER_CYCLES_OUTPUT_ROOT": str(output_root),
            "CX_SPEC_RENDER_CYCLES_TIMEOUT_SECS": str(args.timeout_secs),
            "CX_SPEC_RENDER_CYCLES_DEVICE": args.device,
            "CX_SPEC_RENDER_CYCLES_CANDIDATE_PROFILE": "native",
            "CX_SPEC_RENDER_CYCLES_CANDIDATE_PROFILE_SCOPE": (
                CANDIDATE_PROFILE_NATIVE_SCOPE
            ),
            "CX_SPEC_RENDER_CYCLES_CANDIDATE_PROFILE_AUTH": "",
            # The Rust driver already owns and bounds the renderer process tree.
            "CX_SPEC_RENDER_CYCLES_LOCAL_PROCESS_GROUP": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    request_dir = Path(tempfile.mkdtemp(prefix="sequence-request-", dir=output_root))
    os.chmod(request_dir, 0o700)
    request_path = request_dir / "request.json"
    try:
        _write_new_file(request_path, request_bytes)
        stdout, _, agent_wall_s = _run_command(
            [str(pins["agent"].path), "spec-render-preview", "--input", str(request_path)],
            environment=environment,
            timeout=args.timeout_secs + 10,
            stdout_limit=MAX_AGENT_STDOUT,
            stderr_limit=MAX_AGENT_STDERR,
            label="pinned cx-agent spec-render-preview",
        )
    finally:
        shutil.rmtree(request_dir, ignore_errors=True)

    for name, pin in pins.items():
        _revalidate_pin(pin, name)
    result_value = _strict_json(stdout, "cx-agent result")
    outputs, receipt = _validate_envelope(result_value, len(units))
    frame_rows, renderer_identity, bundle_identity = _validate_selected_outputs(
        outputs,
        units,
        output_root=output_root,
        scene_relative=scene_relative,
        scene_sha256=pins["scene"].sha256,
        args=args,
        pins=pins,
    )

    video = None
    if ffmpeg_pin is not None and ffprobe_pin is not None:
        video = _mux_video(
            frame_rows,
            args=args,
            output_root=output_root,
            ffmpeg_pin=ffmpeg_pin,
            ffprobe_pin=ffprobe_pin,
        )
    _revalidate_frame_sources(frame_rows)
    for name, pin in pins.items():
        _revalidate_pin(pin, name)
    if ffmpeg_pin is not None:
        _revalidate_pin(ffmpeg_pin, "ffmpeg")
    if ffprobe_pin is not None:
        _revalidate_pin(ffprobe_pin, "ffprobe")
    if video is not None:
        video_path = Path(video["path"])
        if video_path.is_symlink() or not video_path.is_file():
            raise SequenceError("published video changed before sequence publication")
        if sha256_file(video_path, max_bytes=MAX_VIDEO_BYTES) != video["sha256"]:
            raise SequenceError("published video digest changed before sequence publication")

    public_frames = [
        {key: value for key, value in row.items() if not key.startswith("_absolute_")}
        for row in frame_rows
    ]
    manifest = {
        "schema_version": SEQUENCE_SCHEMA_VERSION,
        "kind": SEQUENCE_MANIFEST_KIND,
        "preview_only": True,
        "local_unattested": True,
        "billing_eligible": False,
        "nonbillable": True,
        "production_ready": False,
        "receipt_trust": RECEIPT_TRUST,
        "claim_scope": (
            "bounded contiguous-frame local render preview; no artifact attestation, "
            "performance claim, production claim, or audio"
        ),
        "performance_claim": None,
        "speedup_vs_baseline": None,
        "baseline_measured": False,
        "execution_path": "cx-agent spec-render-preview / pinned Cycles backend",
        "execution_timing_s": {
            "agent_wall": round(agent_wall_s, 6),
            "video_packaging_wall": (
                video["timing_s"]["total_wall"] if video is not None else None
            ),
        },
        "request_sha256": request_sha,
        "agent_result_sha256": hashlib.sha256(_canonical_json(result_value)).hexdigest(),
        "config": config,
        "config_sha256": config_sha,
        "scene": {
            "root": str(scene_root),
            "relative_path": scene_relative,
            "sha256": pins["scene"].sha256,
            "bundle": bundle_identity,
        },
        "renderer_identity": renderer_identity,
        "pins": {
            name: {"path": str(pin.path), "sha256": pin.sha256}
            for name, pin in pins.items()
            if name != "scene"
        },
        "receipt_summary": {
            "units": receipt["units"],
            "accepted_units": receipt["accepted_units"],
            "repaired_units": receipt["repaired_units"],
            "quality_gate": receipt["quality_gate"],
            "quality_tier": receipt["quality_tier"],
            "evidence": receipt["evidence"],
            "artifact_verified": receipt["artifact_verified"],
            "draft_cost_s": receipt["draft_cost_s"],
            "verify_cost_s": receipt["verify_cost_s"],
            "repair_cost_s": receipt["repair_cost_s"],
            "overhead_cost_s": receipt["overhead_cost_s"],
            "total_product_time_s": receipt["total_product_time_s"],
        },
        "frames": public_frames,
        "video": video,
    }
    manifest_path, manifest_sha = _write_content_addressed_manifest(output_root, manifest)
    return {
        "schema_version": SEQUENCE_SCHEMA_VERSION,
        "kind": "cx_cycles_preview_sequence_result",
        "preview_only": True,
        "local_unattested": True,
        "billing_eligible": False,
        "production_ready": False,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha,
        "frame_count": len(public_frames),
        "agent_wall_s": round(agent_wall_s, 6),
        "video": video,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = run_sequence(args)
    except Exception as exc:  # noqa: BLE001 - CLI boundary fails closed
        print(f"cx local Cycles sequence rejected: {type(exc).__name__}: {exc}", file=os.sys.stderr)
        return 1
    os.sys.stdout.buffer.write(_canonical_json(result) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
