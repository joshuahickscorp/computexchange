#!/usr/bin/env python3
"""CPU-only temporal prediction frontier over pinned Koro reference frames.

The screen treats full-resolution 4096-SPP frame 9 and frame 10 PNGs as
resident, previously validated inputs and predicts frame 11 without invoking a
renderer.  It benchmarks a direct-prior control and linear RGBA extrapolation
(``clip(2 * f10 - f9)``).  If OpenCV is installed, a bounded Farneback flow arm
is available; an absent module is recorded rather than installed.

Every approximation is explicitly post-hoc and unauthorizable without an
independent target-frame verification.  It is not an exact cache hit: frame 10
and predicted frame 11 have different frame identities even when their pixels
happen to satisfy the quality contract.
"""

from __future__ import annotations

import argparse
from io import BytesIO
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import secrets
import stat
import statistics
import sys
import time
from typing import Any, Callable, NamedTuple, Sequence

import numpy as np
from PIL import Image, UnidentifiedImageError


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import cx_render_quality_v3 as quality  # noqa: E402
import verify_cx_render_quality_v3 as quality_verifier  # noqa: E402


KIND = "cx_koro_temporal_prediction_frontier"
SCHEMA_VERSION = 2
TRIALS = 9
WIDTH = 1080
HEIGHT = 1920
SIZE = (WIDTH, HEIGHT)
REFERENCE_SAMPLES = 4096
BASELINE_SECONDS = 112.396726
CUTOFF_1000X_SECONDS = 0.112396726
MAX_PNG_BYTES = 64 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024 * 1024
REPORT_NAME = "temporal-prediction-frontier.json"
TIMING_EVIDENCE_NAME = "temporal-timing-evidence.json"
TEST_MODULE_PATH = HERE / "test_screen_temporal_prediction_frontier.py"
VALIDATOR_MODULE_PATH = HERE / "validate_temporal_prediction_frontier.py"
COMPOSED_ESTIMATE_POLICY = {
    "construction": (
        "one_time_product_input_validation_plus_resident_trial_median"
    ),
    "integrated_wall_measurement": False,
    "label": "composed_charged_estimate",
    "timing_trust": "local_unattested",
    "statistic_warning": (
        "sum_of_independently_measured_components_not_integrated_median"
    ),
}
BASELINE_EVIDENCE_PATH = Path(
    "/Users/scammermike/.cache/cx-spec-lab/cache-arm/"
    "koro-static-cache-copy-generic-hardened-f9-v-f10-f11-20260712f/"
    "evidence/frame-9-performance-receipt.json"
)
BASELINE_EVIDENCE_BYTES = 8_518
BASELINE_EVIDENCE_SHA256 = (
    "b1ca40bc63288ad8736b76592c24a9baffe168f752c8309ac6925cc2c22f19c8"
)

DEFAULT_FRAME9 = Path(
    "/Users/scammermike/.cache/cx-spec-lab/transfer/"
    "koro-portrait-1080x1920-r4096-d4-v4-f9-20260712/"
    "cycles-preview-4c50873d7e61bbd6b6afdc10913938de/units/"
    "unit-20b9601a46a3394b1f207f0d9a8e2abb2bf2af4267233bcb2bdd03b6649e901a/"
    "baseline.png"
)
DEFAULT_FRAME10 = Path(
    "/Users/scammermike/.cache/cx-spec-lab/frontier/"
    "koro-spatial75-pipelined-calibration-f10-20260712/"
    "cycles-preview-47a57433fb687c564c961746d658b45b/units/"
    "unit-e36487fa21acc716494f92b37a32ca571648bc095f218283d33dbc7445c12f79/"
    "baseline.png"
)
DEFAULT_FRAME11 = Path(
    "/Users/scammermike/.cache/cx-spec-lab/frontier/"
    "koro-spatial75-pipelined-final-f11-20260712/"
    "cycles-preview-0097d49e1fec0b801c135a698820c480/units/"
    "unit-aa3d3002dad7b990fadac0996bf44d9a5f99f1294b09c993e48617a86fcc0bd3/"
    "baseline.png"
)
PINNED_INPUTS = {
    9: {
        "bytes": 8_310_590,
        "sha256": "785044e407ca68c12fd25a874492be8ceb4eb27d7d0631909c4cb100075fad36",
    },
    10: {
        "bytes": 8_310_593,
        "sha256": "59ea20a6019a74e37f58fc6db6018b751c62a9e8b337081d20b6c19e8b4fe262",
    },
    11: {
        "bytes": 8_310_587,
        "sha256": "6f0018443432e38df37996b7b20c26693e61d0cfef55999bbec2f4fb8955fddc",
    },
}


class FrontierError(ValueError):
    """A fail-closed temporal frontier error."""


class DecodedFrame(NamedTuple):
    frame: int
    path: Path
    source_sha256: str
    source_bytes: int
    rgba: np.ndarray
    rgba_sha256: str
    timings_ns: dict[str, int]


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise FrontierError("value is not finite canonical JSON") from exc


def sha256_bytes(value: bytes | memoryview) -> str:
    return hashlib.sha256(value).hexdigest()


def _identity(info: os.stat_result) -> tuple[int, ...]:
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


def _path_identity(path: Path) -> tuple[int, ...]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise FrontierError(f"cannot stat {path.name}") from exc
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise FrontierError(f"{path.name} is not a regular non-symlink file")
    return _identity(info)


def read_immutable(path: Path, *, maximum: int) -> tuple[bytes, tuple[int, ...]]:
    absolute = path.absolute()
    initial = _path_identity(absolute)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(absolute, flags)
    except OSError as exc:
        raise FrontierError(f"cannot open {path.name}") from exc
    chunks: list[bytes] = []
    total = 0
    try:
        before = os.fstat(fd)
        if _identity(before) != initial or not stat.S_ISREG(before.st_mode):
            raise FrontierError(f"{path.name} changed before read")
        if not 1 <= before.st_size <= maximum:
            raise FrontierError(f"{path.name} is outside its byte bound")
        while True:
            chunk = os.read(fd, min(1 << 20, maximum - total + 1))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise FrontierError(f"{path.name} exceeded its byte bound")
        after = os.fstat(fd)
        identity = _identity(after)
        if identity != _identity(before) or total != after.st_size:
            raise FrontierError(f"{path.name} changed during read")
    finally:
        os.close(fd)
    if _path_identity(absolute) != identity:
        raise FrontierError(f"{path.name} path changed after read")
    return b"".join(chunks), identity


def decode_frame(path: Path, frame: int) -> DecodedFrame:
    total_started = time.perf_counter_ns()
    read_started = time.perf_counter_ns()
    data, _file_identity = read_immutable(path, maximum=MAX_PNG_BYTES)
    read_ns = time.perf_counter_ns() - read_started
    hash_started = time.perf_counter_ns()
    source_sha = sha256_bytes(data)
    source_hash_ns = time.perf_counter_ns() - hash_started
    container_started = time.perf_counter_ns()
    try:
        channels, mode = quality._validate_png_container(data, SIZE)
    except quality.QualityInputError as exc:
        raise FrontierError(f"frame {frame} strict PNG rejected: {exc.code}") from exc
    container_ns = time.perf_counter_ns() - container_started
    if mode != "RGBA" or channels != 4:
        raise FrontierError(f"frame {frame} must be RGBA8")
    decode_started = time.perf_counter_ns()
    try:
        with Image.open(BytesIO(data)) as source:
            if (
                source.format != "PNG"
                or source.mode != "RGBA"
                or source.size != SIZE
                or getattr(source, "n_frames", 1) != 1
            ):
                raise FrontierError(f"frame {frame} decoder identity mismatch")
            source.load()
            rgba = np.array(source, dtype=np.uint8, copy=True)
    except FrontierError:
        raise
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise FrontierError(f"frame {frame} decoder failed") from exc
    if rgba.shape != (HEIGHT, WIDTH, 4) or rgba.nbytes != WIDTH * HEIGHT * 4:
        raise FrontierError(f"frame {frame} decoded shape mismatch")
    rgba = np.ascontiguousarray(rgba)
    rgba.setflags(write=False)
    decode_ns = time.perf_counter_ns() - decode_started
    pixel_hash_started = time.perf_counter_ns()
    rgba_sha = sha256_bytes(memoryview(rgba).cast("B"))
    pixel_hash_ns = time.perf_counter_ns() - pixel_hash_started
    timings = {
        "read": read_ns,
        "source_sha256": source_hash_ns,
        "strict_container": container_ns,
        "pillow_decode": decode_ns,
        "decoded_rgba_sha256": pixel_hash_ns,
        "total": time.perf_counter_ns() - total_started,
    }
    return DecodedFrame(
        frame=frame,
        path=path.absolute(),
        source_sha256=source_sha,
        source_bytes=len(data),
        rgba=rgba,
        rgba_sha256=rgba_sha,
        timings_ns=timings,
    )


def assert_pinned_frame(decoded: DecodedFrame) -> None:
    expected = PINNED_INPUTS.get(decoded.frame)
    if expected is None:
        raise FrontierError("frame is outside the pinned input set")
    if (
        decoded.source_sha256 != expected["sha256"]
        or decoded.source_bytes != expected["bytes"]
    ):
        raise FrontierError(f"frame {decoded.frame} does not match its immutable pin")


def direct_prior(_frame9: np.ndarray, frame10: np.ndarray) -> np.ndarray:
    return np.array(frame10, dtype=np.uint8, copy=True, order="C")


def linear_extrapolation(frame9: np.ndarray, frame10: np.ndarray) -> np.ndarray:
    if (
        frame9.shape != (HEIGHT, WIDTH, 4)
        or frame10.shape != frame9.shape
        or frame9.dtype != np.uint8
        or frame10.dtype != np.uint8
    ):
        raise FrontierError("linear predictor input identity mismatch")
    working = frame10.astype(np.int16)
    working -= frame9
    working += frame10
    np.clip(working, 0, 255, out=working)
    return working.astype(np.uint8)


def opencv_flow_predictor() -> tuple[
    Callable[[np.ndarray, np.ndarray], np.ndarray] | None, dict[str, Any]
]:
    try:
        import cv2  # type: ignore[import-not-found]
    except ImportError:
        return None, {
            "available": False,
            "reason": "cv2_module_missing",
            "installed_by_screen": False,
        }

    def predict(frame9: np.ndarray, frame10: np.ndarray) -> np.ndarray:
        gray9 = cv2.cvtColor(frame9[..., :3], cv2.COLOR_RGB2GRAY)
        gray10 = cv2.cvtColor(frame10[..., :3], cv2.COLOR_RGB2GRAY)
        flow = cv2.calcOpticalFlowFarneback(
            gray9,
            gray10,
            None,
            pyr_scale=0.5,
            levels=3,
            winsize=15,
            iterations=3,
            poly_n=5,
            poly_sigma=1.2,
            flags=0,
        )
        yy, xx = np.indices((HEIGHT, WIDTH), dtype=np.float32)
        map_x = xx - flow[..., 0]
        map_y = yy - flow[..., 1]
        return cv2.remap(
            frame10,
            map_x,
            map_y,
            interpolation=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REPLICATE,
        )

    return predict, {
        "available": True,
        "installed_by_screen": False,
        "version": str(cv2.__version__),
        "algorithm": "Farneback f9_to_f10 then backward-sample f10 by one flow step",
        "bounds": {
            "levels": 3,
            "winsize": 15,
            "iterations": 3,
            "poly_n": 5,
            "poly_sigma": 1.2,
        },
    }


def _validate_reconstruction(value: np.ndarray) -> None:
    if (
        type(value) is not np.ndarray
        or value.dtype != np.uint8
        or value.shape != (HEIGHT, WIDTH, 4)
        or not value.flags.c_contiguous
        or value.nbytes != WIDTH * HEIGHT * 4
    ):
        raise FrontierError("reconstruction identity mismatch")


def reconstruction_floor(
    predictor: Callable[[np.ndarray, np.ndarray], np.ndarray],
    frame9: np.ndarray,
    frame10: np.ndarray,
) -> dict[str, Any]:
    samples: list[float] = []
    early_stopped = False
    for index in range(TRIALS):
        started = time.perf_counter_ns()
        candidate = predictor(frame9, frame10)
        _validate_reconstruction(candidate)
        # Touch opposite corners so a future lazy implementation cannot pass
        # without materializing the complete eager NumPy/OpenCV result.
        _ = int(candidate[0, 0, 0]) + int(candidate[-1, -1, -1])
        samples.append((time.perf_counter_ns() - started) / 1e9)
        del candidate
        if index == 2 and min(samples) > CUTOFF_1000X_SECONDS:
            early_stopped = True
            break
    return {
        "early_stop_rule": (
            "stop after three trials when even the observed minimum exceeds "
            "the 1000x cutoff"
        ),
        "early_stopped": early_stopped,
        "samples_s": samples,
        **summarize(samples),
    }


def encode_png0(rgba: np.ndarray) -> bytes:
    _validate_reconstruction(rgba)
    output = BytesIO()
    Image.fromarray(rgba, "RGBA").save(
        output,
        format="PNG",
        optimize=False,
        compress_level=0,
    )
    data = output.getvalue()
    if not 1 <= len(data) <= MAX_PNG_BYTES:
        raise FrontierError("PNG0 output is outside its byte bound")
    return data


def validate_encoded(data: bytes) -> None:
    try:
        channels, mode = quality._validate_png_container(data, SIZE)
    except quality.QualityInputError as exc:
        raise FrontierError(f"encoded PNG strict rejection: {exc.code}") from exc
    if channels != 4 or mode != "RGBA":
        raise FrontierError("encoded PNG mode mismatch")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format != "PNG" or image.mode != "RGBA" or image.size != SIZE:
                raise FrontierError("encoded PNG decoder identity mismatch")
            image.load()
    except FrontierError:
        raise
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise FrontierError("encoded PNG decoder failed") from exc


def _publication_identity(info: os.stat_result) -> tuple[int, ...]:
    """Return fields that remain stable while a hard link is added/removed."""
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_size,
    )


def publish_new(path: Path, data: bytes) -> int:
    """Durably publish *data* without replacing an existing directory entry.

    The stage and destination names are resolved through one retained directory
    descriptor, and the staged inode remains open until publication is durable.
    This detects accidental/concurrent pathname substitution.  As elsewhere in
    the local experiment harness, the containing directory and processes under
    the same UID are part of the trust boundary rather than hostile peers.
    """
    started = time.perf_counter_ns()
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    try:
        directory_fd = os.open(path.parent, directory_flags)
    except OSError as exc:
        raise FrontierError("cannot open publication directory") from exc

    destination_name = path.name
    temporary_name = f".{destination_name}.{secrets.token_hex(16)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    stage_fd = -1
    destination_fd = -1
    stage_present = False
    destination_linked = False
    published = False
    directory_needs_fsync = False
    staged_identity: tuple[int, ...] | None = None
    linked_identity: tuple[int, ...] | None = None
    try:
        try:
            os.stat(
                destination_name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise FrontierError("cannot inspect publication destination") from exc
        else:
            raise FrontierError("publication is no-clobber")

        try:
            stage_fd = os.open(
                temporary_name,
                flags,
                0o600,
                dir_fd=directory_fd,
            )
        except OSError as exc:
            raise FrontierError("cannot create publication temporary") from exc
        stage_present = True
        directory_needs_fsync = True

        with os.fdopen(stage_fd, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
        os.fsync(stage_fd)

        staged_info = os.fstat(stage_fd)
        if not stat.S_ISREG(staged_info.st_mode) or staged_info.st_size != len(data):
            raise FrontierError("publication temporary metadata mismatch")
        staged_identity = _publication_identity(staged_info)
        try:
            staged_path_info = os.stat(
                temporary_name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise FrontierError("publication temporary path changed") from exc
        if _publication_identity(staged_path_info) != staged_identity:
            raise FrontierError("publication temporary identity changed")

        try:
            os.link(
                temporary_name,
                destination_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise FrontierError("publication is no-clobber") from exc
        destination_linked = True
        directory_needs_fsync = True

        try:
            linked_info = os.stat(
                destination_name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise FrontierError("published destination path changed") from exc
        linked_identity = _publication_identity(linked_info)
        if linked_identity != staged_identity:
            raise FrontierError("published destination identity changed")

        destination_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            destination_flags |= os.O_NOFOLLOW
        try:
            destination_fd = os.open(
                destination_name,
                destination_flags,
                dir_fd=directory_fd,
            )
        except OSError as exc:
            raise FrontierError("cannot retain published destination") from exc
        if _publication_identity(os.fstat(destination_fd)) != staged_identity:
            raise FrontierError("published destination descriptor changed")
        os.fsync(destination_fd)

        os.unlink(temporary_name, dir_fd=directory_fd)
        stage_present = False
        os.fsync(directory_fd)
        directory_needs_fsync = False

        final_path_info = os.stat(
            destination_name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            _publication_identity(final_path_info) != staged_identity
            or _publication_identity(os.fstat(destination_fd)) != staged_identity
        ):
            raise FrontierError("published destination changed after directory fsync")
        published = True
    except FrontierError:
        raise
    except OSError as exc:
        raise FrontierError("publication failed") from exc
    finally:
        cleanup_mutated = False
        if not published and destination_linked:
            rollback_identity = linked_identity or staged_identity
            try:
                current = os.stat(
                    destination_name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if (
                    rollback_identity is not None
                    and _publication_identity(current) == rollback_identity
                ):
                    os.unlink(destination_name, dir_fd=directory_fd)
                    cleanup_mutated = True
            except FileNotFoundError:
                pass
            except OSError:
                pass
        if stage_present:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
                cleanup_mutated = True
            except FileNotFoundError:
                pass
            except OSError:
                pass
        if directory_needs_fsync or cleanup_mutated:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
        if destination_fd >= 0:
            os.close(destination_fd)
        if stage_fd >= 0:
            os.close(stage_fd)
        os.close(directory_fd)
    return time.perf_counter_ns() - started


def _type7(values: Sequence[float], quantile: float) -> float:
    if not values:
        raise FrontierError("cannot summarize no samples")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    fraction = position - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def summarize(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {
            "count": 0,
            "median_s": None,
            "p95_s_type7": None,
            "slowest_s": None,
            "minimum_s": None,
        }
    finite = [float(value) for value in values]
    if any(not math.isfinite(value) or value < 0.0 for value in finite):
        raise FrontierError("timing sample is invalid")
    return {
        "count": len(finite),
        "median_s": statistics.median(finite),
        "minimum_s": min(finite),
        "p95_s_type7": _type7(finite, 0.95),
        "slowest_s": max(finite),
    }


def _relative_record(path: Path, root: Path) -> dict[str, Any]:
    identity = _path_identity(path)
    data, after_identity = read_immutable(path, maximum=MAX_PNG_BYTES)
    if identity != after_identity:
        raise FrontierError("published output identity changed")
    return {
        "bytes": len(data),
        "path": path.relative_to(root).as_posix(),
        "sha256": sha256_bytes(data),
    }


def benchmark_arm(
    name: str,
    predictor: Callable[[np.ndarray, np.ndarray], np.ndarray],
    frame9: DecodedFrame,
    frame10: DecodedFrame,
    output_root: Path,
) -> dict[str, Any]:
    floor = reconstruction_floor(predictor, frame9.rgba, frame10.rgba)
    if floor["early_stopped"]:
        return {
            "arm": name,
            "authorization": "cross_frame_approximation_unauthorized",
            "exact_cache_hit": False,
            "reconstruction_floor": floor,
            "status": "stopped_reconstruction_floor_exceeds_1000x_cutoff",
            "trials": [],
        }
    rows: list[dict[str, Any]] = []
    for index in range(TRIALS):
        total_started = time.perf_counter_ns()
        reconstruct_started = time.perf_counter_ns()
        candidate = predictor(frame9.rgba, frame10.rgba)
        _validate_reconstruction(candidate)
        reconstruct_ns = time.perf_counter_ns() - reconstruct_started
        pixel_hash_started = time.perf_counter_ns()
        pixel_sha = sha256_bytes(memoryview(candidate).cast("B"))
        pixel_hash_ns = time.perf_counter_ns() - pixel_hash_started
        encode_started = time.perf_counter_ns()
        encoded = encode_png0(candidate)
        encode_ns = time.perf_counter_ns() - encode_started
        validate_started = time.perf_counter_ns()
        validate_encoded(encoded)
        encoded_sha = sha256_bytes(encoded)
        validate_ns = time.perf_counter_ns() - validate_started
        destination = output_root / f"{name}-trial-{index:02d}.png"
        publish_ns = publish_new(destination, encoded)
        total_ns = time.perf_counter_ns() - total_started
        rows.append(
            {
                "artifact": _relative_record(destination, output_root),
                "decoded_rgba_sha256": pixel_sha,
                "encoded_sha256": encoded_sha,
                "index": index,
                "timings_ns": {
                    "durable_publication": publish_ns,
                    "encoded_validation_and_sha256": validate_ns,
                    "png0_encode": encode_ns,
                    "reconstruction": reconstruct_ns,
                    "reconstruction_sha256": pixel_hash_ns,
                    "resident_total": total_ns,
                },
            }
        )
        del candidate, encoded
    stages = tuple(rows[0]["timings_ns"])
    summaries = {
        stage: summarize([row["timings_ns"][stage] / 1e9 for row in rows])
        for stage in stages
    }
    return {
        "arm": name,
        "authorization": "cross_frame_approximation_unauthorized",
        "exact_cache_hit": False,
        "predeclared_quality_trial": 0,
        "reconstruction_floor": floor,
        "status": "completed",
        "summaries": summaries,
        "trials": rows,
    }


def _write_json_new(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(
        value,
        sort_keys=True,
        indent=2,
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii") + b"\n"
    if len(payload) > MAX_JSON_BYTES:
        raise FrontierError("JSON artifact exceeds its byte bound")
    publish_new(path, payload)


def attach_quality(
    arm: dict[str, Any], output_root: Path, frame11_path: Path
) -> None:
    if arm.get("status") != "completed":
        arm["quality_v3"] = {
            "evaluated": False,
            "reason": "arm_not_completed",
        }
        return
    trial = arm["trials"][arm["predeclared_quality_trial"]]
    candidate = output_root / trial["artifact"]["path"]
    result = quality.evaluate_pngs(candidate, frame11_path, target_size=SIZE)
    proof_path = output_root / f"{arm['arm']}-quality-v3.json"
    _write_json_new(proof_path, result)
    verification = quality_verifier.verify_paths(
        proof_path, candidate, frame11_path
    )
    verification_path = output_root / f"{arm['arm']}-quality-verification.json"
    _write_json_new(verification_path, verification)
    arm["quality_v3"] = {
        "evaluated": True,
        "pass": result["pass"] is True,
        "proof": _relative_json_record(proof_path, output_root),
        "verification": _relative_json_record(verification_path, output_root),
        "verification_pass": verification.get("pass") is True,
        "proof_verified": verification.get("proof_verified") is True,
        "quality_pass": verification.get("quality_pass") is True,
        "failures": result.get("failures"),
        "errors": result.get("errors"),
        "summary": _quality_summary(result),
    }


def _relative_json_record(path: Path, root: Path) -> dict[str, Any]:
    data, _identity_value = read_immutable(path, maximum=MAX_JSON_BYTES)
    return {
        "bytes": len(data),
        "path": path.relative_to(root).as_posix(),
        "sha256": sha256_bytes(data),
    }


def _quality_summary(result: dict[str, Any]) -> dict[str, Any]:
    matte = result.get("mattes", {}).get("black", {})
    metrics = matte.get("metrics", {}) if isinstance(matte, dict) else {}
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
            selected[key] = {"pass": row.get("pass"), "value": row.get("value")}
    alpha = result.get("alpha_agreement", {})
    return {
        "alpha_agreement": (
            {"pass": alpha.get("pass"), "value": alpha.get("value")}
            if isinstance(alpha, dict)
            else None
        ),
        "metrics_black_matte": selected,
        "pass": result.get("pass") is True,
    }


def _prepare_root(path: Path) -> Path:
    if not path.is_absolute():
        raise FrontierError("output root must be absolute")
    if path.exists() or path.is_symlink():
        raise FrontierError("output root must not exist")
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise FrontierError("output parent must be an existing real directory")
    path.mkdir(mode=0o700)
    return path


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frame9", type=Path, default=DEFAULT_FRAME9)
    parser.add_argument("--frame10", type=Path, default=DEFAULT_FRAME10)
    parser.add_argument("--frame11", type=Path, default=DEFAULT_FRAME11)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser.parse_args(argv)


def _baseline_provenance() -> dict[str, Any]:
    data, _identity_value = read_immutable(
        BASELINE_EVIDENCE_PATH,
        maximum=MAX_JSON_BYTES,
    )
    if (
        len(data) != BASELINE_EVIDENCE_BYTES
        or sha256_bytes(data) != BASELINE_EVIDENCE_SHA256
    ):
        raise FrontierError("baseline evidence immutable pin mismatch")
    source = _strict_json(data, BASELINE_EVIDENCE_PATH.name)
    expected_source = {
        "baseline_s": BASELINE_SECONDS,
        "device": "GPU/METAL",
        "frame": 9,
        "receipt_trust": "local_unattested",
        "timing_scope": "resident_steady_state_after_uncharged_warmup",
        "timing_statistics": "fixed-order single trial; no variance estimate",
        "trial_count": 1,
        "variance_estimate": None,
    }
    if any(source.get(key) != value for key, value in expected_source.items()):
        raise FrontierError("baseline evidence provenance mismatch")
    return {
        "baseline_seconds": BASELINE_SECONDS,
        "bytes": BASELINE_EVIDENCE_BYTES,
        "device": source["device"],
        "frame": source["frame"],
        "path": str(BASELINE_EVIDENCE_PATH),
        "receipt_trust": source["receipt_trust"],
        "sha256": BASELINE_EVIDENCE_SHA256,
        "timing_scope": source["timing_scope"],
        "timing_statistics": source["timing_statistics"],
        "trial_count": source["trial_count"],
        "variance_estimate": source["variance_estimate"],
    }


def run(args: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    for label in ("frame9", "frame10", "frame11"):
        path = getattr(args, label)
        if not path.is_absolute():
            raise FrontierError(f"--{label} must be absolute")
        _path_identity(path)
    output_root = _prepare_root(args.output_root)
    frames = {
        frame: decode_frame(path, frame)
        for frame, path in (
            (9, args.frame9),
            (10, args.frame10),
            (11, args.frame11),
        )
    }
    for decoded in frames.values():
        assert_pinned_frame(decoded)
    arms = [
        benchmark_arm(
            "direct-prior-f10", direct_prior, frames[9], frames[10], output_root
        ),
        benchmark_arm(
            "linear-rgba-2xf10-minus-f9",
            linear_extrapolation,
            frames[9],
            frames[10],
            output_root,
        ),
    ]
    flow, flow_environment = opencv_flow_predictor()
    if flow is None:
        arms.append(
            {
                "arm": "opencv-farneback-extrapolation",
                "authorization": "cross_frame_approximation_unauthorized",
                "environment": flow_environment,
                "exact_cache_hit": False,
                "status": "unavailable",
                "trials": [],
            }
        )
    else:
        flow_arm = benchmark_arm(
            "opencv-farneback-extrapolation",
            flow,
            frames[9],
            frames[10],
            output_root,
        )
        flow_arm["environment"] = flow_environment
        arms.append(flow_arm)
    for arm in arms:
        attach_quality(arm, output_root, args.frame11)
    product_validation_ns = (
        frames[9].timings_ns["total"] + frames[10].timings_ns["total"]
    )
    for arm in arms:
        resident = arm.get("summaries", {}).get("resident_total", {})
        median = resident.get("median_s")
        if isinstance(median, (int, float)):
            composed_s = median + product_validation_ns / 1e9
            arm["speed"] = {
                "baseline_seconds": BASELINE_SECONDS,
                "composed_charged_estimate": {
                    **COMPOSED_ESTIMATE_POLICY,
                    "seconds": composed_s,
                    "speedup_x": BASELINE_SECONDS / composed_s,
                },
                "resident_median_s": median,
                "resident_median_speedup_x": BASELINE_SECONDS / median,
                "resident_meets_1000x": median <= CUTOFF_1000X_SECONDS,
                "threshold_1000x_s": CUTOFF_1000X_SECONDS,
            }
    timing_evidence = {
        "arms": [
            {
                "arm": arm["arm"],
                "reconstruction_floor": arm.get("reconstruction_floor"),
                "status": arm["status"],
                "trials": [
                    {
                        "index": trial["index"],
                        "timings_ns": trial["timings_ns"],
                    }
                    for trial in arm.get("trials", [])
                ],
            }
            for arm in arms
        ],
        "baseline_seconds": BASELINE_SECONDS,
        "input_timings_ns": {
            f"frame_{frame}": decoded.timings_ns
            for frame, decoded in frames.items()
        },
        "kind": "cx_koro_temporal_prediction_timing_evidence",
        "schema_version": 1,
        "trial_count": TRIALS,
    }
    timing_evidence_path = output_root / TIMING_EVIDENCE_NAME
    _write_json_new(timing_evidence_path, timing_evidence)
    receipt = {
        "arms": arms,
        "authorization": {
            "cross_frame_approximation_authorized": False,
            "exact_cache_hit": False,
            "independent_target_verification_required": True,
            "product_decision_reference_free": False,
            "reason": (
                "frame 11 is reconstructed from different-frame pixels and "
                "audited post-hoc against a fresh frame-11 target"
            ),
        },
        "baseline_seconds": BASELINE_SECONDS,
        "baseline_provenance": _baseline_provenance(),
        "code_pins": _code_pins(),
        "contract": {
            "quality": quality.CONTRACT_ID,
            "quality_module_sha256": _file_sha256(Path(quality.__file__)),
            "quality_verifier_module_sha256": _file_sha256(
                Path(quality_verifier.__file__)
            ),
            "threshold_1000x_s": CUTOFF_1000X_SECONDS,
        },
        "experimental_only": True,
        "inputs": {
            f"frame_{frame}": {
                "decoded_rgba_sha256": decoded.rgba_sha256,
                "dimensions": [WIDTH, HEIGHT],
                "frame": frame,
                "path": str(decoded.path),
                "pin_verified": True,
                "samples": REFERENCE_SAMPLES,
                "source_bytes": decoded.source_bytes,
                "source_sha256": decoded.source_sha256,
                "timings_ns": decoded.timings_ns,
            }
            for frame, decoded in frames.items()
        },
        "kind": KIND,
        "measurement": {
            "composed_charged_estimate_policy": COMPOSED_ESTIMATE_POLICY,
            "input_validation": {
                "audit_target_frame_11_ns": frames[11].timings_ns["total"],
                "product_frames_9_and_10_ns": product_validation_ns,
                "resident_decodes_reused_across_trials": True,
            },
            "timing_evidence": _relative_json_record(
                timing_evidence_path,
                output_root,
            ),
            "trial_count": TRIALS,
        },
        "optical_flow_environment": flow_environment,
        "receipt_trust": "local_unattested",
        "schema_version": SCHEMA_VERSION,
    }
    canonical_json(receipt)
    report_path = output_root / REPORT_NAME
    _write_json_new(report_path, receipt)
    published = validate_receipt_path(report_path)
    if canonical_json(published) != canonical_json(receipt):
        raise FrontierError("published receipt replay mismatch")
    return receipt, report_path


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _positive_number(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0.0
    ):
        raise FrontierError(f"{label} must be finite and positive")
    return float(value)


def _positive_ns(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise FrontierError(f"{label} must be a positive integer")
    return value


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FrontierError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise FrontierError(f"non-finite JSON constant: {value}")


def _strict_json(data: bytes, label: str) -> dict[str, Any]:
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise FrontierError(f"{label} is not ASCII JSON") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except FrontierError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise FrontierError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise FrontierError(f"{label} must be a JSON object")
    canonical_json(value)
    return value


def _artifact_bytes(
    record: Any,
    output_root: Path,
    *,
    maximum: int,
    expected_name: str | None = None,
) -> tuple[Path, bytes]:
    if not isinstance(record, dict) or set(record) != {"bytes", "path", "sha256"}:
        raise FrontierError("artifact record shape mismatch")
    relative = record.get("path")
    if not isinstance(relative, str):
        raise FrontierError("artifact path must be a string")
    pure = PurePosixPath(relative)
    if (
        pure.is_absolute()
        or len(pure.parts) != 1
        or pure.parts[0] in {"", ".", ".."}
        or relative != pure.name
        or (expected_name is not None and relative != expected_name)
    ):
        raise FrontierError("artifact path is not a confined flat filename")
    byte_count = record.get("bytes")
    if type(byte_count) is not int or not 1 <= byte_count <= maximum:
        raise FrontierError("artifact byte count is invalid")
    if not _is_sha256(record.get("sha256")):
        raise FrontierError("artifact SHA-256 is invalid")
    path = output_root / relative
    data, _identity_value = read_immutable(path, maximum=maximum)
    if len(data) != byte_count or sha256_bytes(data) != record["sha256"]:
        raise FrontierError(f"artifact bytes or SHA-256 mismatch: {relative}")
    return path, data


def _validate_summary(
    observed: Any,
    samples: Any,
    label: str,
) -> list[float]:
    if not isinstance(samples, list) or not samples:
        raise FrontierError(f"{label} samples are missing")
    values = [_positive_number(value, f"{label} sample") for value in samples]
    expected = summarize(values)
    if not isinstance(observed, dict) or canonical_json(observed) != canonical_json(
        expected
    ):
        raise FrontierError(f"{label} summary mismatch")
    return values


def _validate_input_record(record: Any, frame: int) -> None:
    expected_keys = {
        "decoded_rgba_sha256",
        "dimensions",
        "frame",
        "path",
        "pin_verified",
        "samples",
        "source_bytes",
        "source_sha256",
        "timings_ns",
    }
    if not isinstance(record, dict) or set(record) != expected_keys:
        raise FrontierError(f"frame {frame} input shape mismatch")
    pin = PINNED_INPUTS[frame]
    if (
        record.get("dimensions") != [WIDTH, HEIGHT]
        or record.get("frame") != frame
        or record.get("pin_verified") is not True
        or record.get("samples") != REFERENCE_SAMPLES
        or record.get("source_bytes") != pin["bytes"]
        or record.get("source_sha256") != pin["sha256"]
        or not _is_sha256(record.get("decoded_rgba_sha256"))
        or not isinstance(record.get("path"), str)
        or not Path(record["path"]).is_absolute()
    ):
        raise FrontierError(f"frame {frame} input identity mismatch")
    timings = record.get("timings_ns")
    timing_keys = {
        "decoded_rgba_sha256",
        "pillow_decode",
        "read",
        "source_sha256",
        "strict_container",
        "total",
    }
    if not isinstance(timings, dict) or set(timings) != timing_keys:
        raise FrontierError(f"frame {frame} timing shape mismatch")
    values = {
        key: _positive_ns(value, f"frame {frame} {key}")
        for key, value in timings.items()
    }
    if values["total"] < sum(
        value for key, value in values.items() if key != "total"
    ):
        raise FrontierError(f"frame {frame} timing arithmetic mismatch")


def _validate_reconstruction_floor(
    value: Any,
    label: str,
    *,
    expected_stopped: bool,
) -> None:
    if not isinstance(value, dict):
        raise FrontierError(f"{label} reconstruction floor is missing")
    samples = value.get("samples_s")
    expected = {
        "early_stop_rule": (
            "stop after three trials when even the observed minimum exceeds "
            "the 1000x cutoff"
        ),
        "early_stopped": expected_stopped,
        "samples_s": samples,
        **summarize(
            [_positive_number(sample, f"{label} floor sample") for sample in samples]
            if isinstance(samples, list)
            else []
        ),
    }
    if (
        not isinstance(samples, list)
        or len(samples) != (3 if expected_stopped else TRIALS)
        or (
            expected_stopped
            and min(float(sample) for sample in samples)
            <= CUTOFF_1000X_SECONDS
        )
        or canonical_json(value) != canonical_json(expected)
    ):
        raise FrontierError(f"{label} reconstruction floor mismatch")


def _validate_speed(
    speed: Any,
    resident_median_s: float,
    product_validation_ns: int,
    label: str,
) -> None:
    composed_s = resident_median_s + product_validation_ns / 1e9
    expected = {
        "baseline_seconds": BASELINE_SECONDS,
        "composed_charged_estimate": {
            **COMPOSED_ESTIMATE_POLICY,
            "seconds": composed_s,
            "speedup_x": BASELINE_SECONDS / composed_s,
        },
        "resident_median_s": resident_median_s,
        "resident_median_speedup_x": BASELINE_SECONDS / resident_median_s,
        "resident_meets_1000x": resident_median_s <= CUTOFF_1000X_SECONDS,
        "threshold_1000x_s": CUTOFF_1000X_SECONDS,
    }
    if not isinstance(speed, dict) or canonical_json(speed) != canonical_json(expected):
        raise FrontierError(f"{label} speed arithmetic or scope mismatch")


def _timing_evidence_from_receipt(
    receipt: dict[str, Any],
) -> dict[str, Any]:
    return {
        "arms": [
            {
                "arm": arm["arm"],
                "reconstruction_floor": arm.get("reconstruction_floor"),
                "status": arm["status"],
                "trials": [
                    {
                        "index": trial["index"],
                        "timings_ns": trial["timings_ns"],
                    }
                    for trial in arm.get("trials", [])
                ],
            }
            for arm in receipt["arms"]
        ],
        "baseline_seconds": receipt["baseline_seconds"],
        "input_timings_ns": {
            label: record["timings_ns"]
            for label, record in receipt["inputs"].items()
        },
        "kind": "cx_koro_temporal_prediction_timing_evidence",
        "schema_version": 1,
        "trial_count": receipt["measurement"]["trial_count"],
    }


def validate_receipt(receipt: dict[str, Any], *, output_root: Path) -> None:
    """Strictly replay one measured temporal frontier receipt and its files."""
    top_keys = {
        "arms",
        "authorization",
        "baseline_seconds",
        "baseline_provenance",
        "code_pins",
        "contract",
        "experimental_only",
        "inputs",
        "kind",
        "measurement",
        "optical_flow_environment",
        "receipt_trust",
        "schema_version",
    }
    if not isinstance(receipt, dict) or set(receipt) != top_keys:
        raise FrontierError("receipt shape mismatch")
    if receipt.get("kind") != KIND or receipt.get("schema_version") != SCHEMA_VERSION:
        raise FrontierError("receipt identity mismatch")
    if receipt.get("experimental_only") is not True:
        raise FrontierError("receipt experimental scope mismatch")
    if receipt.get("receipt_trust") != "local_unattested":
        raise FrontierError("receipt timing trust mismatch")
    if receipt.get("baseline_seconds") != BASELINE_SECONDS:
        raise FrontierError("receipt baseline mismatch")
    if canonical_json(receipt.get("baseline_provenance")) != canonical_json(
        _baseline_provenance()
    ):
        raise FrontierError("receipt baseline provenance mismatch")
    if canonical_json(receipt.get("code_pins")) != canonical_json(_code_pins()):
        raise FrontierError("receipt code pin mismatch")
    expected_contract = {
        "quality": quality.CONTRACT_ID,
        "quality_module_sha256": _file_sha256(Path(quality.__file__)),
        "quality_verifier_module_sha256": _file_sha256(
            Path(quality_verifier.__file__)
        ),
        "threshold_1000x_s": CUTOFF_1000X_SECONDS,
    }
    if canonical_json(receipt.get("contract")) != canonical_json(expected_contract):
        raise FrontierError("receipt quality contract pin mismatch")
    expected_authorization = {
        "cross_frame_approximation_authorized": False,
        "exact_cache_hit": False,
        "independent_target_verification_required": True,
        "product_decision_reference_free": False,
        "reason": (
            "frame 11 is reconstructed from different-frame pixels and "
            "audited post-hoc against a fresh frame-11 target"
        ),
    }
    if canonical_json(receipt.get("authorization")) != canonical_json(
        expected_authorization
    ):
        raise FrontierError("receipt authorization mismatch")
    if (
        not output_root.is_absolute()
        or not output_root.is_dir()
        or output_root.is_symlink()
    ):
        raise FrontierError("receipt artifact root must be an absolute real directory")

    inputs = receipt.get("inputs")
    if not isinstance(inputs, dict) or set(inputs) != {
        "frame_9",
        "frame_10",
        "frame_11",
    }:
        raise FrontierError("receipt input set mismatch")
    for frame in (9, 10, 11):
        _validate_input_record(inputs[f"frame_{frame}"], frame)

    measurement = receipt.get("measurement")
    if not isinstance(measurement, dict) or set(measurement) != {
        "composed_charged_estimate_policy",
        "input_validation",
        "timing_evidence",
        "trial_count",
    }:
        raise FrontierError("receipt measurement shape mismatch")
    if canonical_json(measurement["composed_charged_estimate_policy"]) != canonical_json(
        COMPOSED_ESTIMATE_POLICY
    ) or measurement.get("trial_count") != TRIALS:
        raise FrontierError("receipt measurement policy mismatch")
    input_validation = measurement.get("input_validation")
    expected_product_ns = (
        inputs["frame_9"]["timings_ns"]["total"]
        + inputs["frame_10"]["timings_ns"]["total"]
    )
    expected_input_validation = {
        "audit_target_frame_11_ns": inputs["frame_11"]["timings_ns"]["total"],
        "product_frames_9_and_10_ns": expected_product_ns,
        "resident_decodes_reused_across_trials": True,
    }
    if canonical_json(input_validation) != canonical_json(expected_input_validation):
        raise FrontierError("receipt input-validation arithmetic mismatch")

    _flow_predictor, current_flow_environment = opencv_flow_predictor()
    if canonical_json(receipt.get("optical_flow_environment")) != canonical_json(
        current_flow_environment
    ):
        raise FrontierError("receipt optical-flow environment mismatch")
    arms = receipt.get("arms")
    if not isinstance(arms, list) or [
        arm.get("arm") if isinstance(arm, dict) else None for arm in arms
    ] != [
        "direct-prior-f10",
        "linear-rgba-2xf10-minus-f9",
        "opencv-farneback-extrapolation",
    ]:
        raise FrontierError("receipt arm identity or order mismatch")
    completed_arms: list[dict[str, Any]] = []
    timing_keys = {
        "durable_publication",
        "encoded_validation_and_sha256",
        "png0_encode",
        "reconstruction",
        "reconstruction_sha256",
        "resident_total",
    }
    for arm in arms:
        label = arm["arm"]
        common = {
            "arm",
            "authorization",
            "exact_cache_hit",
            "quality_v3",
            "status",
            "trials",
        }
        if label == "opencv-farneback-extrapolation":
            common.add("environment")
            if canonical_json(arm.get("environment")) != canonical_json(
                current_flow_environment
            ):
                raise FrontierError("optical-flow arm environment mismatch")
        if arm.get("status") == "unavailable":
            if (
                label != "opencv-farneback-extrapolation"
                or set(arm) != common
                or arm.get("authorization")
                != "cross_frame_approximation_unauthorized"
                or arm.get("exact_cache_hit") is not False
                or canonical_json(arm.get("environment"))
                != canonical_json(current_flow_environment)
                or arm.get("trials") != []
                or canonical_json(arm.get("quality_v3"))
                != canonical_json(
                    {"evaluated": False, "reason": "arm_not_completed"}
                )
            ):
                raise FrontierError("unavailable optical-flow arm mismatch")
            continue
        if arm.get("status") == "stopped_reconstruction_floor_exceeds_1000x_cutoff":
            stopped_keys = common | {"reconstruction_floor"}
            if (
                set(arm) != stopped_keys
                or arm.get("authorization")
                != "cross_frame_approximation_unauthorized"
                or arm.get("exact_cache_hit") is not False
                or arm.get("trials") != []
                or canonical_json(arm.get("quality_v3"))
                != canonical_json(
                    {"evaluated": False, "reason": "arm_not_completed"}
                )
            ):
                raise FrontierError(f"{label} stopped arm shape mismatch")
            _validate_reconstruction_floor(
                arm.get("reconstruction_floor"),
                label,
                expected_stopped=True,
            )
            continue
        completed_keys = common | {
            "predeclared_quality_trial",
            "reconstruction_floor",
            "speed",
            "summaries",
        }
        if (
            arm.get("status") != "completed"
            or set(arm) != completed_keys
            or arm.get("authorization")
            != "cross_frame_approximation_unauthorized"
            or arm.get("exact_cache_hit") is not False
            or arm.get("predeclared_quality_trial") != 0
        ):
            raise FrontierError(f"{label} completed arm shape mismatch")
        _validate_reconstruction_floor(
            arm.get("reconstruction_floor"),
            label,
            expected_stopped=False,
        )
        trials = arm.get("trials")
        if not isinstance(trials, list) or len(trials) != TRIALS:
            raise FrontierError(f"{label} trial count mismatch")
        samples_by_stage: dict[str, list[float]] = {
            stage: [] for stage in timing_keys
        }
        decoded_hashes: set[str] = set()
        encoded_hashes: set[str] = set()
        for index, trial in enumerate(trials):
            if not isinstance(trial, dict) or set(trial) != {
                "artifact",
                "decoded_rgba_sha256",
                "encoded_sha256",
                "index",
                "timings_ns",
            }:
                raise FrontierError(f"{label} trial shape mismatch")
            if trial.get("index") != index:
                raise FrontierError(f"{label} trial index mismatch")
            expected_artifact_name = f"{label}-trial-{index:02d}.png"
            record = trial.get("artifact")
            if (
                not isinstance(record, dict)
                or set(record) != {"bytes", "path", "sha256"}
                or record.get("path") != expected_artifact_name
                or trial.get("encoded_sha256") != record.get("sha256")
                or not _is_sha256(trial.get("decoded_rgba_sha256"))
            ):
                raise FrontierError(f"{label} trial artifact identity mismatch")
            timings = trial.get("timings_ns")
            if not isinstance(timings, dict) or set(timings) != timing_keys:
                raise FrontierError(f"{label} trial timing shape mismatch")
            ns_values = {
                stage: _positive_ns(value, f"{label} {stage}")
                for stage, value in timings.items()
            }
            if ns_values["resident_total"] < sum(
                value
                for stage, value in ns_values.items()
                if stage != "resident_total"
            ):
                raise FrontierError(f"{label} trial timing arithmetic mismatch")
            for stage, value in ns_values.items():
                samples_by_stage[stage].append(value / 1e9)
            decoded_hashes.add(trial["decoded_rgba_sha256"])
            encoded_hashes.add(trial["encoded_sha256"])
        if len(decoded_hashes) != 1 or len(encoded_hashes) != 1:
            raise FrontierError(f"{label} deterministic trial identity mismatch")
        expected_summaries = {
            stage: summarize(samples) for stage, samples in samples_by_stage.items()
        }
        if canonical_json(arm.get("summaries")) != canonical_json(expected_summaries):
            raise FrontierError(f"{label} timing summary mismatch")
        resident_median = expected_summaries["resident_total"]["median_s"]
        _validate_speed(
            arm.get("speed"),
            resident_median,
            expected_product_ns,
            label,
        )
        quality_block = arm.get("quality_v3")
        if not isinstance(quality_block, dict) or set(quality_block) != {
            "errors",
            "evaluated",
            "failures",
            "pass",
            "proof",
            "proof_verified",
            "quality_pass",
            "summary",
            "verification",
            "verification_pass",
        }:
            raise FrontierError(f"{label} quality block shape mismatch")
        if quality_block.get("evaluated") is not True or any(
            type(quality_block.get(key)) is not bool
            for key in (
                "pass",
                "verification_pass",
                "proof_verified",
                "quality_pass",
            )
        ) or not isinstance(quality_block.get("errors"), list) or not isinstance(
            quality_block.get("failures"), list
        ):
            raise FrontierError(f"{label} quality decision mismatch")
        completed_arms.append(arm)

    timing_path, timing_data = _artifact_bytes(
        measurement.get("timing_evidence"),
        output_root,
        maximum=MAX_JSON_BYTES,
        expected_name=TIMING_EVIDENCE_NAME,
    )
    timing_value = _strict_json(timing_data, timing_path.name)
    if canonical_json(timing_value) != canonical_json(
        _timing_evidence_from_receipt(receipt)
    ):
        raise FrontierError("timing evidence does not match receipt")

    decoded_inputs: dict[int, DecodedFrame] = {}
    for frame in (9, 10, 11):
        record = inputs[f"frame_{frame}"]
        decoded = decode_frame(Path(record["path"]), frame)
        assert_pinned_frame(decoded)
        if (
            decoded.source_bytes != record["source_bytes"]
            or decoded.source_sha256 != record["source_sha256"]
            or decoded.rgba_sha256 != record["decoded_rgba_sha256"]
        ):
            raise FrontierError(f"frame {frame} current input replay mismatch")
        decoded_inputs[frame] = decoded

    predictors: dict[str, Callable[[np.ndarray, np.ndarray], np.ndarray]] = {
        "direct-prior-f10": direct_prior,
        "linear-rgba-2xf10-minus-f9": linear_extrapolation,
    }
    if _flow_predictor is not None:
        predictors["opencv-farneback-extrapolation"] = _flow_predictor
    reference_path = Path(inputs["frame_11"]["path"])
    for arm in completed_arms:
        label = arm["arm"]
        predictor = predictors.get(label)
        if predictor is None:
            raise FrontierError(f"{label} predictor is unavailable for replay")
        expected_rgba = predictor(
            decoded_inputs[9].rgba,
            decoded_inputs[10].rgba,
        )
        _validate_reconstruction(expected_rgba)
        expected_rgba_sha = sha256_bytes(memoryview(expected_rgba).cast("B"))
        expected_png = encode_png0(expected_rgba)
        expected_png_sha = sha256_bytes(expected_png)
        for index, trial in enumerate(arm["trials"]):
            candidate_path, candidate_data = _artifact_bytes(
                trial["artifact"],
                output_root,
                maximum=MAX_PNG_BYTES,
                expected_name=f"{label}-trial-{index:02d}.png",
            )
            validate_encoded(candidate_data)
            if (
                candidate_data != expected_png
                or trial["decoded_rgba_sha256"] != expected_rgba_sha
                or trial["encoded_sha256"] != expected_png_sha
            ):
                raise FrontierError(f"{label} candidate replay mismatch")
            if index != arm["predeclared_quality_trial"]:
                continue
            quality_block = arm["quality_v3"]
            proof_path, proof_data = _artifact_bytes(
                quality_block["proof"],
                output_root,
                maximum=MAX_JSON_BYTES,
                expected_name=f"{label}-quality-v3.json",
            )
            verification_path, verification_data = _artifact_bytes(
                quality_block["verification"],
                output_root,
                maximum=MAX_JSON_BYTES,
                expected_name=f"{label}-quality-verification.json",
            )
            proof = _strict_json(proof_data, proof_path.name)
            verification = _strict_json(verification_data, verification_path.name)
            proof_inputs = proof.get("inputs")
            proof_candidate = (
                proof_inputs.get("candidate")
                if isinstance(proof_inputs, dict)
                else None
            )
            proof_reference = (
                proof_inputs.get("reference")
                if isinstance(proof_inputs, dict)
                else None
            )
            if (
                not isinstance(proof_inputs, dict)
                or not isinstance(proof_candidate, dict)
                or not isinstance(proof_reference, dict)
                or proof_inputs.get("target_dimensions") != [WIDTH, HEIGHT]
                or proof_candidate.get("bytes")
                != trial["artifact"]["bytes"]
                or proof_candidate.get("sha256")
                != trial["artifact"]["sha256"]
                or proof_reference.get("bytes")
                != inputs["frame_11"]["source_bytes"]
                or proof_reference.get("sha256")
                != inputs["frame_11"]["source_sha256"]
            ):
                raise FrontierError(f"{label} quality proof input binding mismatch")
            try:
                replayed = quality_verifier.verify_paths(
                    proof_path,
                    candidate_path,
                    reference_path,
                )
            except Exception as exc:
                raise FrontierError(f"{label} quality verifier replay failed") from exc
            if canonical_json(replayed) != canonical_json(verification):
                raise FrontierError(f"{label} quality verifier artifact mismatch")
            expected_quality_block = {
                "errors": proof.get("errors"),
                "evaluated": True,
                "failures": proof.get("failures"),
                "pass": proof.get("pass") is True,
                "proof": quality_block["proof"],
                "proof_verified": replayed.get("proof_verified") is True,
                "quality_pass": replayed.get("quality_pass") is True,
                "summary": _quality_summary(proof),
                "verification": quality_block["verification"],
                "verification_pass": replayed.get("pass") is True,
            }
            if canonical_json(quality_block) != canonical_json(
                expected_quality_block
            ):
                raise FrontierError(f"{label} embedded quality mismatch")


def validate_receipt_path(path: Path) -> dict[str, Any]:
    if not path.is_absolute() or path.name != REPORT_NAME:
        raise FrontierError("receipt path must be the absolute canonical report name")
    data, _identity_value = read_immutable(path, maximum=MAX_JSON_BYTES)
    receipt = _strict_json(data, path.name)
    validate_receipt(receipt, output_root=path.parent)
    return receipt


def _file_sha256(path: Path) -> str:
    data, _identity_value = read_immutable(path, maximum=MAX_JSON_BYTES)
    return sha256_bytes(data)


def _code_pins() -> dict[str, dict[str, str]]:
    paths = {
        "screen_module": Path(__file__).resolve(),
        "test_module": TEST_MODULE_PATH.resolve(),
        "validator_module": VALIDATOR_MODULE_PATH.resolve(),
    }
    return {
        label: {"path": str(path), "sha256": _file_sha256(path)}
        for label, path in paths.items()
    }


def main(argv: Sequence[str] | None = None) -> int:
    try:
        receipt, report = run(_parse_args(argv))
        print(
            json.dumps(
                {
                    "arms": [
                        {
                            "arm": arm["arm"],
                            "quality_pass": arm.get("quality_v3", {}).get("pass"),
                            "resident_median_s": arm.get("summaries", {})
                            .get("resident_total", {})
                            .get("median_s"),
                            "status": arm["status"],
                        }
                        for arm in receipt["arms"]
                    ],
                    "kind": KIND,
                    "ok": True,
                    "report": str(report),
                },
                sort_keys=True,
            )
        )
        return 0
    except BaseException as exc:
        print(
            json.dumps(
                {
                    "error": f"{type(exc).__name__}: {exc}"[:4000],
                    "kind": KIND,
                    "ok": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
