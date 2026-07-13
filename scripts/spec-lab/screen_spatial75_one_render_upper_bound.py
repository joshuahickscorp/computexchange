#!/usr/bin/env python3
"""Measure the frozen spatial75 recipe without its independent authorization.

This bounded screen retains the frozen frame-11 810x1440 4+4-SPP policy, but
executes only the first 4-SPP draft render in each measured trial.  The retained
backend snapshot is converted, reconstructed with RGB and alpha BICUBIC, encoded
as PNG compression level zero, validated, and durably published.  The omitted
second 4-SPP render and reference-free pair gate make every output explicitly
unauthorizable; this is only a latency upper-bound experiment.

``render`` performs one uncharged full-path warmup and seven measured GPU
trials, shuts Blender down, then writes an open report.  ``finalize`` runs
quality-v3 and its independent proof verifier for every predeclared delivery
against the already-pinned fresh frame-11 reference and closes the report.
Quality is measurement-only and cannot retroactively authorize publication.
"""

from __future__ import annotations

import argparse
import ctypes
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
from typing import Any, Sequence


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import run_spatial75_cycles_frontier as frozen


SCHEMA_VERSION = 1
KIND = "cx_spatial75_one_render_upper_bound"
REPORT_NAME = "spatial75-one-render-upper-bound.json"
FRAME = 11
TRIAL_COUNT = 7
LOW_SIZE = (810, 1440)
OUTPUT_SIZE = (1080, 1920)
DRAFT_SAMPLES = 4
DECLARED_VERIFY_SAMPLES = 4
PNG_COMPRESSION = 0

EXPECTED_SCENE_SHA256 = (
    "12fdca959492d4cb59a06ca837e2e7baa87d1ef6ac67648a88758089a7b14c07"
)
EXPECTED_BLENDER_SHA256 = (
    "b5737da97b0e164cc18e227be115c4ea2791d11e9577f7e302c73f4871f9249c"
)
EXPECTED_REFERENCE_RECEIPT_SHA256 = (
    "13649a6bc6687c4f5ee487a5185a8cf806b0159e19c4ede60c5ca0fffeb16bc6"
)
EXPECTED_REFERENCE_SHA256 = (
    "5807fbd9383a87f6e2e9f823c6f109fbaa75fc3b004300cd2272c2b2da8bd76c"
)
EXPECTED_REFERENCE_BYTES = 8_310_593
EXPECTED_REFERENCE_BASELINE_S = 112.49490395799876
BASELINE_1000X_BUDGET_S = EXPECTED_REFERENCE_BASELINE_S / 1000.0
EXPECTED_SPATIAL_POLICY_SHA256 = (
    "7bef862be6a09c68aaa395cc3396841fb031998c67e79779faaeb1aff7c0a70e"
)


class UpperBoundError(ValueError):
    """The bounded measurement or one of its identity checks failed closed."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: str | Path) -> str:
    return frozen.sha256_file(path)


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
        raise UpperBoundError("report is not finite canonical JSON") from exc


def _round9(value: float) -> float:
    if not math.isfinite(value) or value < 0.0:
        raise UpperBoundError("timing is not finite and nonnegative")
    return float(f"{value:.9f}")


def _finite_positive(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0.0
    ):
        raise UpperBoundError(f"{label} must be finite and positive")
    return float(value)


def _type7_quantile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(_finite_positive(value, "timing sample") for value in values)
    if not ordered or not 0.0 <= quantile <= 1.0:
        raise UpperBoundError("invalid quantile input")
    position = (len(ordered) - 1) * quantile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def timing_statistics(values: Sequence[float]) -> dict[str, float | int]:
    samples = [_finite_positive(value, "timing sample") for value in values]
    if len(samples) != TRIAL_COUNT:
        raise UpperBoundError("timing statistic requires seven trials")
    return {
        "count": len(samples),
        "median_s": statistics.median(samples),
        "p95_s_type7": _type7_quantile(samples, 0.95),
        "minimum_s": min(samples),
        "maximum_s": max(samples),
        "population_variance_s2": statistics.pvariance(samples),
    }


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size)


def _regular_identity(info: os.stat_result, label: str) -> tuple[int, int, int, int]:
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        raise UpperBoundError(f"{label} is not a nonempty regular file")
    return _file_identity(info)


def _sha256_fd(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    for chunk in iter(lambda: os.read(descriptor, 1024 * 1024), b""):
        digest.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def _atomic_exchange(directory_fd: int, left: str, right: str) -> None:
    """Atomically swap two names in one retained directory descriptor."""

    libc = ctypes.CDLL(None, use_errno=True)
    left_bytes = os.fsencode(left)
    right_bytes = os.fsencode(right)
    exchange_flag = 0x00000002
    if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        operation = libc.renameatx_np
        operation.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        operation.restype = ctypes.c_int
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        operation = libc.renameat2
        operation.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        operation.restype = ctypes.c_int
    else:
        raise UpperBoundError("atomic no-clobber JSON exchange is unavailable")
    if operation(
        directory_fd,
        left_bytes,
        directory_fd,
        right_bytes,
        exchange_flag,
    ) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _name_identity(directory_fd: int, name: str) -> tuple[int, int, int, int] | None:
    try:
        return _file_identity(os.stat(name, dir_fd=directory_fd, follow_symlinks=False))
    except FileNotFoundError:
        return None


def _unlink_name_if_identity(
    directory_fd: int,
    name: str,
    expected: tuple[int, int, int, int] | None,
) -> None:
    if expected is not None and _name_identity(directory_fd, name) == expected:
        os.unlink(name, dir_fd=directory_fd)


def _write_json_atomic(
    path: Path,
    value: dict[str, Any],
    *,
    replace: bool,
    expected_sha256: str | None = None,
) -> None:
    """Durably publish JSON while retaining every participating descriptor.

    New files use a hard-link no-clobber commit. Replacements use an atomic
    name exchange, then prove that the displaced inode is exactly the retained
    prior report. Same-UID mutation of the retained directory is outside this
    local experiment's trust boundary, but every observed substitution fails
    closed and is rolled back when both exchanged names still match.
    """

    encoded = json.dumps(
        value,
        sort_keys=True,
        indent=2,
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii") + b"\n"
    if not path.parent.is_dir() or path.parent.is_symlink() or not path.name:
        raise UpperBoundError("JSON parent is not a safe directory")
    if replace and (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        raise UpperBoundError("replacement requires the exact prior JSON digest")
    temporary_name = f".{path.name}.{secrets.token_hex(16)}.tmp"
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    directory_fd = os.open(path.parent, directory_flags)
    stage_fd = -1
    old_fd = -1
    destination_fd = -1
    displaced_fd = -1
    stage_identity: tuple[int, int, int, int] | None = None
    old_identity: tuple[int, int, int, int] | None = None
    linked = False
    exchanged = False
    committed = False
    try:
        if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
            raise UpperBoundError("JSON parent descriptor is not a directory")
        stage_fd = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
        view = memoryview(encoded)
        while view:
            written = os.write(stage_fd, view)
            if written <= 0:
                raise UpperBoundError("JSON stage write made no progress")
            view = view[written:]
        os.fsync(stage_fd)
        stage_info = os.fstat(stage_fd)
        stage_identity = _regular_identity(stage_info, "JSON stage")
        if stage_info.st_size != len(encoded) or _sha256_fd(stage_fd) != sha256_bytes(encoded):
            raise UpperBoundError("JSON stage descriptor identity mismatch")

        if replace:
            read_flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                read_flags |= os.O_NOFOLLOW
            old_fd = os.open(path.name, read_flags, dir_fd=directory_fd)
            old_identity = _regular_identity(os.fstat(old_fd), "prior JSON report")
            if (
                _name_identity(directory_fd, path.name) != old_identity
                or _sha256_fd(old_fd) != expected_sha256
            ):
                raise UpperBoundError("prior JSON report changed before closure")
            _atomic_exchange(directory_fd, temporary_name, path.name)
            exchanged = True
            destination_fd = os.open(path.name, read_flags, dir_fd=directory_fd)
            displaced_fd = os.open(temporary_name, read_flags, dir_fd=directory_fd)
            if (
                _regular_identity(os.fstat(destination_fd), "closed JSON report")
                != stage_identity
                or _regular_identity(os.fstat(displaced_fd), "displaced JSON report")
                != old_identity
            ):
                raise UpperBoundError("JSON exchange participant was substituted")
        else:
            try:
                os.link(
                    temporary_name,
                    path.name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise UpperBoundError("refusing to replace JSON output") from exc
            linked = True
            read_flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                read_flags |= os.O_NOFOLLOW
            destination_fd = os.open(path.name, read_flags, dir_fd=directory_fd)
            if (
                _regular_identity(os.fstat(destination_fd), "published JSON report")
                != stage_identity
            ):
                raise UpperBoundError("JSON hard-link participant was substituted")

        os.fsync(directory_fd)
        if (
            _name_identity(directory_fd, path.name) != stage_identity
            or _sha256_fd(destination_fd) != sha256_bytes(encoded)
        ):
            raise UpperBoundError("published JSON path changed during directory sync")
        if replace and _name_identity(directory_fd, temporary_name) != old_identity:
            raise UpperBoundError("displaced JSON path changed during directory sync")
        _unlink_name_if_identity(
            directory_fd,
            temporary_name,
            old_identity if replace else stage_identity,
        )
        os.fsync(directory_fd)
        if _name_identity(directory_fd, path.name) != stage_identity:
            raise UpperBoundError("published JSON path changed after commit cleanup")
        committed = True
    finally:
        if not committed:
            if (
                replace
                and exchanged
                and _name_identity(directory_fd, path.name) == stage_identity
                and _name_identity(directory_fd, temporary_name) == old_identity
            ):
                try:
                    _atomic_exchange(directory_fd, path.name, temporary_name)
                    exchanged = False
                except OSError:
                    pass
            if not replace and linked:
                _unlink_name_if_identity(directory_fd, path.name, stage_identity)
            _unlink_name_if_identity(directory_fd, temporary_name, stage_identity)
        for descriptor in (displaced_fd, destination_fd, old_fd, stage_fd):
            if descriptor >= 0:
                os.close(descriptor)
        os.close(directory_fd)


def _clear_incomplete_finalization_artifacts(
    output_root: Path, trial_count: int
) -> None:
    """Make an open report restartable after an interrupted finalization.

    Quality proofs are deterministic derivatives of the still-open report and
    its pinned artifacts. They are not authoritative until the report is
    atomically closed, so a later finalization may remove only those reserved
    regular-file names and recompute them from scratch.
    """

    if (
        isinstance(trial_count, bool)
        or not isinstance(trial_count, int)
        or not 1 <= trial_count <= 32
    ):
        raise UpperBoundError("finalization trial count is outside the closed bound")
    try:
        path_info = output_root.lstat()
    except OSError as exc:
        raise UpperBoundError("finalization root is unavailable") from exc
    if stat.S_ISLNK(path_info.st_mode) or not stat.S_ISDIR(path_info.st_mode):
        raise UpperBoundError("finalization root must be a real directory")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    directory_fd = os.open(output_root, flags)
    removed = False
    try:
        opened = os.fstat(directory_fd)
        if (path_info.st_dev, path_info.st_ino) != (opened.st_dev, opened.st_ino):
            raise UpperBoundError("finalization root identity changed")
        reserved = [
            name
            for index in range(trial_count)
            for name in (
                f"quality-v3-trial-{index:02d}.json",
                f"quality-v3-verification-trial-{index:02d}.json",
            )
        ]
        existing: list[tuple[str, tuple[int, int, int, int]]] = []
        for name in reserved:
            identity = _name_identity(directory_fd, name)
            if identity is None:
                continue
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise UpperBoundError(
                    "incomplete finalization artifact is not a private regular file"
                )
            existing.append((name, identity))
        for name, identity in existing:
            if _name_identity(directory_fd, name) != identity:
                raise UpperBoundError(
                    "incomplete finalization artifact changed during cleanup"
                )
            os.unlink(name, dir_fd=directory_fd)
            removed = True
        if removed:
            os.fsync(directory_fd)
    except BaseException:
        if removed:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
        raise
    finally:
        os.close(directory_fd)


def _relative_record(path: Path, root: Path) -> dict[str, Any]:
    record = frozen._relative_record(path, root)
    return {
        "path": record["path"],
        "bytes": record["bytes"],
        "sha256": record["sha256"],
    }


def _resolve_record(root: Path, record: dict[str, Any], label: str) -> Path:
    if not isinstance(record, dict) or set(record) != {"path", "bytes", "sha256"}:
        raise UpperBoundError(f"{label} artifact record shape mismatch")
    relative = (
        PurePosixPath(record["path"])
        if isinstance(record.get("path"), str)
        else None
    )
    if (
        relative is None
        or relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
        or type(record["bytes"]) is not int
        or record["bytes"] <= 0
        or not isinstance(record["sha256"], str)
        or len(record["sha256"]) != 64
        or any(character not in "0123456789abcdef" for character in record["sha256"])
    ):
        raise UpperBoundError(f"{label} artifact record is unsafe")
    try:
        resolved_root = root.resolve(strict=True)
        if not resolved_root.is_dir():
            raise UpperBoundError(f"{label} artifact root is unavailable")
        path = resolved_root.joinpath(*relative.parts)
        before = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_root)
        observed = resolved.stat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise UpperBoundError(f"{label} artifact escaped its root") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or path.is_symlink()
        or _file_identity(before) != _file_identity(observed)
    ):
        raise UpperBoundError(f"{label} artifact is unavailable")
    digest = sha256_file(resolved)
    try:
        after = path.lstat()
    except OSError as exc:
        raise UpperBoundError(f"{label} artifact changed while hashing") from exc
    if (
        observed.st_size != record["bytes"]
        or digest != record["sha256"]
        or _file_identity(after) != _file_identity(before)
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_ctime_ns != before.st_ctime_ns
    ):
        raise UpperBoundError(f"{label} artifact identity mismatch")
    return resolved


def _reject_json_constant(value: str) -> None:
    raise UpperBoundError(f"non-finite JSON constant {value!r}")


def _reject_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise UpperBoundError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_bytes(),
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
        raise UpperBoundError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise UpperBoundError(f"{label} is not a JSON object")
    canonical_json(value)
    return value


def _code_pins() -> dict[str, str]:
    return {
        **frozen._code_pins(),
        "one_render_screen_sha256": sha256_file(Path(__file__).resolve()),
        "one_render_screen_test_sha256": sha256_file(
            HERE / "test_screen_spatial75_one_render_upper_bound.py"
        ),
    }


def _reference_binding(receipt_path: Path) -> dict[str, Any]:
    if not receipt_path.is_absolute() or not receipt_path.is_file() or receipt_path.is_symlink():
        raise UpperBoundError("reference receipt must be an absolute regular file")
    raw = receipt_path.read_bytes()
    if sha256_bytes(raw) != EXPECTED_REFERENCE_RECEIPT_SHA256:
        raise UpperBoundError("fresh frame-11 reference receipt pin mismatch")
    try:
        receipt = json.loads(
            raw,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpperBoundError("fresh frame-11 reference receipt is malformed") from exc
    try:
        frozen.validate_receipt(
            receipt,
            output_root=receipt_path.parent.resolve(strict=True),
        )
    except (frozen.FrontierError, OSError, RuntimeError) as exc:
        raise UpperBoundError("fresh frame-11 reference receipt failed replay") from exc
    if (
        receipt.get("kind") != frozen.KIND
        or receipt.get("frame") != FRAME
        or receipt.get("quality_pass") is not True
        or receipt.get("scene", {}).get("sha256") != EXPECTED_SCENE_SHA256
        or receipt.get("candidate", {}).get("draft_samples") != DRAFT_SAMPLES
        or receipt.get("candidate", {}).get("verify_samples")
        != DECLARED_VERIFY_SAMPLES
        or receipt.get("candidate", {}).get("spatial75", {})
        .get("gate", {})
        .get("policy_sha256")
        != EXPECTED_SPATIAL_POLICY_SHA256
        or receipt.get("baseline_s") != EXPECTED_REFERENCE_BASELINE_S
    ):
        raise UpperBoundError("fresh frame-11 reference receipt contract mismatch")
    artifact = receipt.get("artifacts", {}).get("baseline")
    if not isinstance(artifact, dict):
        raise UpperBoundError("fresh frame-11 reference artifact is absent")
    reference = _resolve_record(receipt_path.parent, artifact, "reference")
    if (
        artifact["sha256"] != EXPECTED_REFERENCE_SHA256
        or artifact["bytes"] != EXPECTED_REFERENCE_BYTES
    ):
        raise UpperBoundError("fresh frame-11 reference artifact pin mismatch")
    return {
        "artifact": {
            "bytes": artifact["bytes"],
            "path": str(reference),
            "sha256": artifact["sha256"],
        },
        "baseline_s": receipt["baseline_s"],
        "receipt": {
            "bytes": len(raw),
            "path": str(receipt_path.resolve(strict=True)),
            "sha256": sha256_bytes(raw),
        },
    }


def _measurement_only_publish(spatial: Any, prepared: Any, destination: Path) -> dict[str, Any]:
    """Publish sealed prepared bytes with no authorization claim or pair gate."""
    started = time.perf_counter_ns()
    validation_started = time.perf_counter_ns()
    spatial._validate_prepared_seal(prepared)
    validation_ns = time.perf_counter_ns() - validation_started
    publish_ns = spatial._publish_new(destination, prepared.encoded_png)
    total_ns = time.perf_counter_ns() - started
    return {
        "authorization": False,
        "independent_gate_executed": False,
        "publication_method": "sealed_prepared_bytes_atomic_no_clobber_measurement_only",
        "timings_ns": {
            "seal_validation": validation_ns,
            "publish": publish_ns,
            "total": total_ns,
        },
    }


def _prepare_and_publish(
    spatial: Any,
    snapshot: Any,
    delivery_path: Path,
) -> tuple[Any, dict[str, Any]]:
    conversion_started = time.perf_counter()
    decoded = frozen._decoded_from_backend_snapshot(spatial, snapshot)
    conversion_s = time.perf_counter() - conversion_started
    preparation_started = time.perf_counter()
    prepared = spatial.prepare_decoded_draft(decoded)
    preparation_s = time.perf_counter() - preparation_started
    publication_started = time.perf_counter()
    publication = _measurement_only_publish(spatial, prepared, delivery_path)
    publication_s = time.perf_counter() - publication_started
    return prepared, {
        "immutable_snapshot_conversion_s": conversion_s,
        "preparation_s": preparation_s,
        "publication_s": publication_s,
        "snapshot": frozen._snapshot_receipt(snapshot),
        "prepared": prepared.receipt(),
        "publication": publication,
    }


def _validate_worker_trace(backend: Any, context: dict[str, Any]) -> dict[str, Any]:
    expected = [
        {
            "command_id": 1,
            "frame": FRAME,
            "phase": "draft",
            "mutation": backend.RESIDENT_POLICY_BROAD,
        }
    ] + [
        {
            "command_id": index + 2,
            "frame": FRAME,
            "phase": "draft",
            "mutation": frozen.RESIDENT_POLICY,
        }
        for index in range(TRIAL_COUNT)
    ]
    observed = context.get("resident_mutation_history")
    worker = getattr(backend, "_WORKER", None)
    if (
        observed != expected
        or not isinstance(worker, dict)
        or worker.get("commands") != TRIAL_COUNT + 1
        or worker.get("process") is None
        or worker["process"].poll() is not None
        or worker.get("key") != backend._worker_key(context)
    ):
        raise UpperBoundError("one-render resident worker trace mismatch")
    return {
        "commands": expected,
        "independent_verify_commands": 0,
        "target_render_commands": TRIAL_COUNT,
        "warmup_render_commands": 1,
        "worker_reused": True,
    }


def _parse_render(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--blender", type=Path, default=frozen.DEFAULT_BLENDER)
    parser.add_argument("--reference-receipt", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--timeout-secs", type=int, default=600)
    args = parser.parse_args(argv)
    for name in ("scene", "blender", "reference_receipt", "output_root"):
        if not getattr(args, name).is_absolute():
            parser.error(f"--{name.replace('_', '-')} must be absolute")
    if not 1 <= args.timeout_secs <= 600:
        parser.error("--timeout-secs must be in [1,600]")
    return args


def _parse_finalize(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.report.is_absolute():
        parser.error("--report must be absolute")
    return args


def parse_outer(argv: Sequence[str]) -> tuple[str, argparse.Namespace]:
    outer = argparse.ArgumentParser(description=__doc__)
    outer.add_argument("command", choices=("render", "finalize"))
    if not argv:
        raise UpperBoundError("expected render or finalize command")
    if argv[0] in {"-h", "--help"}:
        outer.parse_args(argv)
        raise AssertionError("argparse should have exited")
    if argv[0] == "render":
        return "render", _parse_render(argv[1:])
    if argv[0] == "finalize":
        return "finalize", _parse_finalize(argv[1:])
    raise UpperBoundError(f"unknown command: {argv[0]}")


def _validate_render_args(args: argparse.Namespace) -> tuple[Path, Path, dict[str, Any]]:
    scene = args.scene.resolve(strict=True)
    blender = args.blender.resolve(strict=True)
    if scene.suffix != ".blend" or scene.is_symlink() or not scene.is_file():
        raise UpperBoundError("scene must be a regular lowercase .blend file")
    if blender.is_symlink() or not blender.is_file() or not os.access(blender, os.X_OK):
        raise UpperBoundError("Blender must be a regular executable file")
    if sha256_file(scene) != EXPECTED_SCENE_SHA256:
        raise UpperBoundError("frozen Koro scene pin mismatch")
    if sha256_file(blender) != EXPECTED_BLENDER_SHA256:
        raise UpperBoundError("frozen Blender executable pin mismatch")
    if args.output_root.exists() or args.output_root.is_symlink():
        raise UpperBoundError("output root must not already exist")
    if not args.output_root.parent.is_dir() or args.output_root.parent.is_symlink():
        raise UpperBoundError("output root parent must be a safe existing directory")
    reference = _reference_binding(args.reference_receipt)
    return scene, blender, reference


def render_screen(args: argparse.Namespace) -> dict[str, Any]:
    scene, blender, reference = _validate_render_args(args)
    args.output_root.mkdir(mode=0o700)
    output_root = args.output_root.resolve(strict=True)
    report_path = output_root / REPORT_NAME
    starting_pins = _code_pins()
    scene_sha = sha256_file(scene)
    blender_sha = sha256_file(blender)

    backend: Any | None = None
    context: dict[str, Any] | None = None
    environment_before: dict[str, str | None] = {}
    worker_closed = False
    try:
        import cx_agent_render_preview_driver as driver
        import cx_render_spatial75_v1 as spatial
        import run_local_cycles_spec_benchmark as local_benchmark

        runtime = spatial.runtime_identity()
        if (
            runtime.get("module_sha256") != starting_pins["spatial75_module_sha256"]
            or runtime.get("operators")
            != {
                "PIL.Image.Resampling.BICUBIC": 3,
                "expected_bicubic_enum": 3,
            }
            or spatial.POLICY_DESCRIPTOR.get("transform")
            != {
                "rgb_representation": "straight (not alpha-premultiplied)",
                "rgb_operator": "PIL.Image.Resampling.BICUBIC",
                "rgb_operator_enum": 3,
                "alpha_operator": "PIL.Image.Resampling.BICUBIC",
                "alpha_operator_enum": 3,
                "post_filter": None,
            }
            or spatial.PNG_COMPRESSION_LEVEL != PNG_COMPRESSION
        ):
            raise UpperBoundError("frozen spatial75 runtime/operator pin mismatch")

        renderer_identity = {
            **local_benchmark.blender_identity(blender),
            "executable_sha256": blender_sha,
        }
        if renderer_identity.get("official_signed_executable") is not True:
            raise UpperBoundError("renderer is not the official signed Blender executable")
        capability = secrets.token_hex(32)
        environment_updates = {
            "CX_SPEC_RENDER_PREVIEW_BACKEND": str(frozen.BACKEND_PATH),
            "CX_SPEC_RENDER_PREVIEW_BACKEND_SHA256": starting_pins["backend_sha256"],
            "CX_SPEC_RENDER_PREVIEW_CORE_SHA256": starting_pins[
                "controller_core_sha256"
            ],
            "CX_SPEC_RENDER_PREVIEW_ADAPTER_SHA256": starting_pins[
                "controller_adapter_sha256"
            ],
            "CX_SPEC_RENDER_CYCLES_BLENDER": str(blender),
            "CX_SPEC_RENDER_CYCLES_BLENDER_SHA256": blender_sha,
            "CX_SPEC_RENDER_CYCLES_SCENE_ROOT": str(scene.parent),
            "CX_SPEC_RENDER_CYCLES_OUTPUT_ROOT": str(output_root),
            "CX_SPEC_RENDER_CYCLES_TIMEOUT_SECS": str(args.timeout_secs),
            "CX_SPEC_RENDER_CYCLES_DEVICE": "METAL",
            "CX_SPEC_RENDER_CYCLES_LOCAL_PROCESS_GROUP": "1",
            frozen.CANDIDATE_PROFILE_ENV: frozen.CANDIDATE_PROFILE,
            frozen.CANDIDATE_PROFILE_SCOPE_ENV: (
                frozen.CANDIDATE_PROFILE_BENCHMARK_SCOPE
            ),
            frozen.CANDIDATE_PROFILE_AUTH_ENV: capability,
            frozen.RESIDENT_POLICY_ENV: frozen.RESIDENT_POLICY,
        }
        environment_before = {
            key: os.environ.get(key) for key in environment_updates
        }
        os.environ.update(environment_updates)

        core, _adapter, core_sha, adapter_sha = driver._load_pinned_controllers()
        if (
            core_sha != starting_pins["controller_core_sha256"]
            or adapter_sha != starting_pins["controller_adapter_sha256"]
        ):
            raise UpperBoundError("loaded controller pin mismatch")
        backend = driver._load_backend(
            frozen.BACKEND_PATH, starting_pins["backend_sha256"]
        )
        unit = frozen._unit(core, scene, scene_sha, FRAME, LOW_SIZE, capability)
        context = backend._context_for_unit(unit)

        warmup_started = time.perf_counter()
        warm_path, warm_manifest, warm_sha, warm_endpoint_s, warm_snapshot = (
            frozen._render_with_manifest(
                backend,
                context,
                "draft",
                DRAFT_SAMPLES,
                context["seeds"]["draft"],
                "upper-bound-warmup-draft.png",
                "upper-bound-warmup-draft-manifest.json",
                "upper-bound-warmup-draft",
                retain_validated_png=True,
            )
        )
        if warm_snapshot is None:
            raise UpperBoundError("warmup retained snapshot is unavailable")
        warm_delivery = context["unit_dir"] / "upper-bound-warmup-delivery.png"
        warm_prepared, warm_pipeline = _prepare_and_publish(
            spatial, warm_snapshot, warm_delivery
        )
        warmup_s = time.perf_counter() - warmup_started

        trials: list[dict[str, Any]] = []
        for trial_index in range(TRIAL_COUNT):
            prefix = f"upper-bound-trial-{trial_index:02d}"
            trial_started = time.perf_counter()
            (
                draft_path,
                draft_manifest,
                draft_sha,
                endpoint_s,
                snapshot,
            ) = frozen._render_with_manifest(
                backend,
                context,
                "draft",
                DRAFT_SAMPLES,
                context["seeds"]["draft"],
                f"{prefix}-draft.png",
                f"{prefix}-draft-manifest.json",
                f"{prefix}-draft",
                retain_validated_png=True,
            )
            if snapshot is None:
                raise UpperBoundError("trial retained snapshot is unavailable")
            delivery_path = context["unit_dir"] / f"{prefix}-delivery.png"
            prepared, pipeline = _prepare_and_publish(
                spatial, snapshot, delivery_path
            )
            wall_s = time.perf_counter() - trial_started
            components = {
                "target_endpoint_and_manifest_s": endpoint_s,
                "immutable_snapshot_conversion_s": pipeline[
                    "immutable_snapshot_conversion_s"
                ],
                "reconstruction_encode_validate_bind_s": pipeline["preparation_s"],
                "measurement_only_atomic_publication_s": pipeline["publication_s"],
            }
            attributed = sum(components.values())
            trials.append(
                {
                    "artifacts_internal": {
                        "delivery_path": delivery_path,
                        "draft_manifest": draft_manifest,
                        "draft_path": draft_path,
                    },
                    "components_s": components,
                    "draft_sha256": draft_sha,
                    "pipeline": pipeline,
                    "prepared": prepared,
                    "trial_index": trial_index,
                    "unattributed_python_s": max(0.0, wall_s - attributed),
                    "wall_s": wall_s,
                }
            )
            backend._discard_retained_validated_pngs(context)

        frozen._assert_no_retained_pngs(backend, [context])
        trace = _validate_worker_trace(backend, context)
        backend._shutdown_worker()
        worker_closed = True
        print(
            json.dumps(
                {
                    "event": "blender_exit",
                    "gpu_free": True,
                    "kind": KIND,
                    "target_trials_complete": TRIAL_COUNT,
                },
                sort_keys=True,
            ),
            flush=True,
        )

        frozen._validate_render_config(
            context["unit_dir"] / "upper-bound-warmup-draft-render-config.json",
            phase="draft",
            frame=FRAME,
            size=LOW_SIZE,
            samples=DRAFT_SAMPLES,
            sample_offset=0,
            seed=context["seeds"]["draft"],
            output=warm_path,
        )
        frozen._validate_render_manifest(
            warm_manifest,
            phase="draft",
            frame=FRAME,
            size=LOW_SIZE,
            samples=DRAFT_SAMPLES,
            sample_offset=0,
            seed=context["seeds"]["draft"],
            mutation=backend.RESIDENT_POLICY_BROAD,
            artifact=warm_path,
            artifact_sha256=warm_sha,
            output_root=output_root,
        )
        trial_receipts: list[dict[str, Any]] = []
        for trial in trials:
            index = trial["trial_index"]
            prefix = f"upper-bound-trial-{index:02d}"
            internal = trial["artifacts_internal"]
            frozen._validate_render_config(
                context["unit_dir"] / f"{prefix}-draft-render-config.json",
                phase="draft",
                frame=FRAME,
                size=LOW_SIZE,
                samples=DRAFT_SAMPLES,
                sample_offset=0,
                seed=context["seeds"]["draft"],
                output=internal["draft_path"],
            )
            frozen._validate_render_manifest(
                internal["draft_manifest"],
                phase="draft",
                frame=FRAME,
                size=LOW_SIZE,
                samples=DRAFT_SAMPLES,
                sample_offset=0,
                seed=context["seeds"]["draft"],
                mutation=frozen.RESIDENT_POLICY,
                artifact=internal["draft_path"],
                artifact_sha256=trial["draft_sha256"],
                output_root=output_root,
            )
            draft_record = _relative_record(internal["draft_path"], output_root)
            manifest_record = _relative_record(internal["draft_manifest"], output_root)
            config_record = _relative_record(
                context["unit_dir"] / f"{prefix}-draft-render-config.json",
                output_root,
            )
            delivery_record = _relative_record(internal["delivery_path"], output_root)
            prepared_receipt = trial["prepared"].receipt()
            if (
                draft_record["sha256"] != trial["draft_sha256"]
                or delivery_record["sha256"]
                != prepared_receipt["prepared_output"]["sha256"]
                or delivery_record["bytes"]
                != prepared_receipt["prepared_output"]["bytes"]
                or trial["pipeline"]["publication"]["authorization"] is not False
                or trial["pipeline"]["publication"]["independent_gate_executed"]
                is not False
            ):
                raise UpperBoundError("trial prepared/delivery identity mismatch")
            frozen._validate_snapshot_receipt(
                trial["pipeline"]["snapshot"],
                artifact_sha256=draft_record["sha256"],
                artifact_bytes=draft_record["bytes"],
            )
            trial_receipts.append(
                {
                    "artifacts": {
                        "delivery": delivery_record,
                        "draft": draft_record,
                        "draft_config": config_record,
                        "draft_manifest": manifest_record,
                    },
                    "components_s": {
                        key: _round9(value)
                        for key, value in trial["components_s"].items()
                    },
                    "pipeline": trial["pipeline"],
                    "predeclared_for_quality": True,
                    "trial_index": index,
                    "unattributed_python_s": _round9(
                        trial["unattributed_python_s"]
                    ),
                    "wall_s": _round9(trial["wall_s"]),
                }
            )

        warm_artifacts = {
            "delivery": _relative_record(warm_delivery, output_root),
            "draft": _relative_record(warm_path, output_root),
            "draft_manifest": _relative_record(warm_manifest, output_root),
        }
        if (
            warm_artifacts["delivery"]["sha256"]
            != warm_prepared.output_sha256
            or warm_pipeline["publication"]["authorization"] is not False
        ):
            raise UpperBoundError("warmup publication identity mismatch")

        wall_samples = [trial["wall_s"] for trial in trial_receipts]
        component_names = tuple(trial_receipts[0]["components_s"])
        statistics_by_component = {
            name: timing_statistics(
                [trial["components_s"][name] for trial in trial_receipts]
            )
            for name in component_names
        }
        policy = {
            "declared_product_sampling": {
                "draft": {"sample_offset": 0, "samples": DRAFT_SAMPLES},
                "independent_verify": {
                    "sample_offset": DRAFT_SAMPLES,
                    "samples": DECLARED_VERIFY_SAMPLES,
                },
                "sample_ranges_disjoint": True,
            },
            "measurement_sampling": {
                "independent_verify_executed": False,
                "sample_offset": 0,
                "samples": DRAFT_SAMPLES,
                "target_render_invocations_per_trial": 1,
            },
            "output": {
                "alpha_resample": "BICUBIC",
                "compression_level": PNG_COMPRESSION,
                "dimensions": list(OUTPUT_SIZE),
                "format": "PNG",
                "optimize": False,
                "rgb_resample": "BICUBIC",
            },
            "spatial_policy_sha256": EXPECTED_SPATIAL_POLICY_SHA256,
        }
        report = {
            "authorization": {
                "independent_pair_gate_executed": False,
                "independent_verify_render_executed": False,
                "measurement_only": True,
                "production_change_authorized": False,
                "publication_authorized": False,
                "reason": (
                    "the second disjoint 4-SPP render and spatial75 agreement "
                    "gate were deliberately excluded"
                ),
            },
            "evidence": "measured",
            "execution_trace": trace,
            "finalization": {
                "closed": False,
                "quality_v3_run": False,
                "quality_verifier_run": False,
            },
            "frame": FRAME,
            "kind": KIND,
            "pins": {**starting_pins, "blender_executable_sha256": blender_sha},
            "policy": policy,
            "policy_sha256": sha256_bytes(canonical_json(policy)),
            "reference": reference,
            "renderer_identity": renderer_identity,
            "scene": {"path": str(scene), "sha256": scene_sha},
            "schema_version": SCHEMA_VERSION,
            "timing": {
                "component_statistics": statistics_by_component,
                "quality_and_verification_charged": False,
                "trial_wall_statistics": timing_statistics(wall_samples),
                "warmup_charged": False,
            },
            "trial_count": TRIAL_COUNT,
            "trials": trial_receipts,
            "warmup": {
                "artifacts": warm_artifacts,
                "charged": False,
                "pipeline": warm_pipeline,
                "target_render_invocations": 1,
                "wall_s": _round9(warmup_s),
                "target_endpoint_and_manifest_s": _round9(warm_endpoint_s),
            },
        }
        validate_open_report(report, output_root)
        if (
            _code_pins() != starting_pins
            or sha256_file(scene) != scene_sha
            or sha256_file(blender) != blender_sha
        ):
            raise UpperBoundError("pinned inputs changed during render screen")
        _write_json_atomic(report_path, report, replace=False)
        return report
    finally:
        if backend is not None:
            if context is not None:
                backend._discard_retained_validated_pngs(context)
            if not worker_closed:
                backend._shutdown_worker()
        if environment_before:
            frozen._restore_environment(environment_before)


def _validate_reference_block(reference: Any) -> None:
    if not isinstance(reference, dict) or set(reference) != {
        "artifact",
        "baseline_s",
        "receipt",
    }:
        raise UpperBoundError("one-render reference shape mismatch")
    receipt = reference.get("receipt")
    if not isinstance(receipt, dict) or set(receipt) != {"bytes", "path", "sha256"}:
        raise UpperBoundError("one-render reference receipt shape mismatch")
    if not isinstance(receipt.get("path"), str):
        raise UpperBoundError("one-render reference receipt path mismatch")
    receipt_path = Path(receipt["path"])
    observed = _reference_binding(receipt_path)
    if canonical_json(observed) != canonical_json(reference):
        raise UpperBoundError("one-render reference binding mismatch")


def _validate_timing_block(report: dict[str, Any]) -> None:
    timing = report.get("timing")
    if not isinstance(timing, dict) or set(timing) != {
        "component_statistics",
        "quality_and_verification_charged",
        "trial_wall_statistics",
        "warmup_charged",
    }:
        raise UpperBoundError("one-render timing block shape mismatch")
    if (
        timing["quality_and_verification_charged"] is not False
        or timing["warmup_charged"] is not False
    ):
        raise UpperBoundError("one-render timing charge scope mismatch")
    expected_components = {
        "target_endpoint_and_manifest_s",
        "immutable_snapshot_conversion_s",
        "reconstruction_encode_validate_bind_s",
        "measurement_only_atomic_publication_s",
    }
    component_statistics = timing.get("component_statistics")
    if not isinstance(component_statistics, dict) or set(component_statistics) != expected_components:
        raise UpperBoundError("one-render component statistics shape mismatch")
    trials = report["trials"]
    expected_wall = timing_statistics([trial["wall_s"] for trial in trials])
    if canonical_json(timing.get("trial_wall_statistics")) != canonical_json(expected_wall):
        raise UpperBoundError("one-render wall statistics mismatch")
    for name in expected_components:
        expected = timing_statistics([trial["components_s"][name] for trial in trials])
        if canonical_json(component_statistics.get(name)) != canonical_json(expected):
            raise UpperBoundError(f"one-render {name} statistics mismatch")


def _expected_conclusion(report: dict[str, Any]) -> dict[str, Any]:
    wall_stats = timing_statistics(
        [float(trial["wall_s"]) for trial in report["trials"]]
    )
    baseline_s = _finite_positive(
        report.get("reference", {}).get("baseline_s"),
        "reference baseline",
    )
    median_wall = float(wall_stats["median_s"])
    p95_wall = float(wall_stats["p95_s_type7"])
    slowest_wall = float(wall_stats["maximum_s"])
    fastest_wall = float(wall_stats["minimum_s"])
    pass_count = sum(
        trial.get("quality", {}).get("result", {}).get("pass") is True
        for trial in report["trials"]
    )
    budget = baseline_s / 1000.0
    return {
        "all_trials_quality_v3_pass": pass_count == TRIAL_COUNT,
        "baseline_s": baseline_s,
        "fastest_wall_s": fastest_wall,
        "independent_pair_gate_executed": False,
        "measurement_only": True,
        "median_speedup_x": baseline_s / median_wall,
        "median_wall_s": median_wall,
        "p95_speedup_x": baseline_s / p95_wall,
        "p95_wall_s_type7": p95_wall,
        "production_change_authorized": False,
        "publication_authorized": False,
        "quality_v3_pass_count": pass_count,
        "reaches_1000x_authorized": False,
        "slowest_speedup_x": baseline_s / slowest_wall,
        "slowest_wall_s": slowest_wall,
        "speedup_scope": "fresh_frame11_4096spp_baseline_over_one_render_wall",
        "upper_bound_reaches_1000x_measurement_only": (
            pass_count == TRIAL_COUNT and median_wall <= budget
        ),
        "upper_bound_speedup_at_median_x": baseline_s / median_wall,
    }


def _expected_budget(report: dict[str, Any]) -> dict[str, Any]:
    wall_stats = timing_statistics(
        [float(trial["wall_s"]) for trial in report["trials"]]
    )
    baseline_s = _finite_positive(
        report.get("reference", {}).get("baseline_s"),
        "reference baseline",
    )
    budget = baseline_s / 1000.0
    fastest_wall = float(wall_stats["minimum_s"])
    median_wall = float(wall_stats["median_s"])
    p95_wall = float(wall_stats["p95_s_type7"])
    return {
        "budget_s": budget,
        "fastest_gap_s": fastest_wall - budget,
        "fastest_multiple_over_budget": fastest_wall / budget,
        "median_gap_s": median_wall - budget,
        "median_multiple_over_budget": median_wall / budget,
        "p95_gap_s": p95_wall - budget,
        "p95_multiple_over_budget": p95_wall / budget,
        "source_baseline_s": baseline_s,
    }


def _recompute_quality_verification(
    proof_path: Path,
    candidate_path: Path,
    reference_path: Path,
) -> dict[str, Any]:
    try:
        import verify_cx_render_quality_v3 as verifier

        replayed = verifier.verify_paths(proof_path, candidate_path, reference_path)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        raise UpperBoundError("independent quality-v3 replay failed") from exc
    if not isinstance(replayed, dict):
        raise UpperBoundError("independent quality-v3 replay returned invalid data")
    return replayed


def validate_open_report(report: dict[str, Any], output_root: Path) -> None:
    expected_keys = {
        "authorization",
        "evidence",
        "execution_trace",
        "finalization",
        "frame",
        "kind",
        "pins",
        "policy",
        "policy_sha256",
        "reference",
        "renderer_identity",
        "scene",
        "schema_version",
        "timing",
        "trial_count",
        "trials",
        "warmup",
    }
    if not isinstance(report, dict) or set(report) != expected_keys:
        raise UpperBoundError("one-render open report shape mismatch")
    if report.get("kind") != KIND or report.get("schema_version") != SCHEMA_VERSION:
        raise UpperBoundError("one-render report identity mismatch")
    if report.get("frame") != FRAME or report.get("trial_count") != TRIAL_COUNT:
        raise UpperBoundError("one-render report scope mismatch")
    authorization = report.get("authorization")
    scene = report.get("scene")
    renderer = report.get("renderer_identity")
    pins = report.get("pins")
    if (
        not isinstance(authorization, dict)
        or not isinstance(scene, dict)
        or not isinstance(renderer, dict)
        or not isinstance(pins, dict)
    ):
        raise UpperBoundError("one-render measured identity shape mismatch")
    if (
        report.get("evidence") != "measured"
        or pins
        != {**_code_pins(), "blender_executable_sha256": EXPECTED_BLENDER_SHA256}
        or scene.get("sha256") != EXPECTED_SCENE_SHA256
        or renderer.get("official_signed_executable") is not True
        or renderer.get("executable_sha256") != EXPECTED_BLENDER_SHA256
    ):
        raise UpperBoundError("one-render measured identity mismatch")
    _validate_reference_block(report.get("reference"))
    if (
        authorization.get("independent_pair_gate_executed") is not False
        or authorization.get("independent_verify_render_executed") is not False
        or authorization.get("measurement_only") is not True
        or authorization.get("production_change_authorized") is not False
        or authorization.get("publication_authorized") is not False
    ):
        raise UpperBoundError("one-render authorization claim broadened")
    policy = report.get("policy")
    if (
        not isinstance(policy, dict)
        or report.get("policy_sha256") != sha256_bytes(canonical_json(policy))
        or policy.get("declared_product_sampling", {}).get("draft")
        != {"sample_offset": 0, "samples": DRAFT_SAMPLES}
        or policy.get("declared_product_sampling", {}).get("independent_verify")
        != {"sample_offset": DRAFT_SAMPLES, "samples": DECLARED_VERIFY_SAMPLES}
        or policy.get("measurement_sampling", {}).get(
            "independent_verify_executed"
        )
        is not False
        or policy.get("measurement_sampling", {}).get(
            "target_render_invocations_per_trial"
        )
        != 1
        or policy.get("measurement_sampling", {}).get("sample_offset") != 0
        or policy.get("measurement_sampling", {}).get("samples") != DRAFT_SAMPLES
        or policy.get("declared_product_sampling", {}).get(
            "sample_ranges_disjoint"
        )
        is not True
        or policy.get("output", {}).get("rgb_resample") != "BICUBIC"
        or policy.get("output", {}).get("alpha_resample") != "BICUBIC"
        or policy.get("output", {}).get("compression_level") != PNG_COMPRESSION
        or policy.get("output", {}).get("dimensions") != list(OUTPUT_SIZE)
        or policy.get("output", {}).get("format") != "PNG"
        or policy.get("output", {}).get("optimize") is not False
        or policy.get("spatial_policy_sha256") != EXPECTED_SPATIAL_POLICY_SHA256
    ):
        raise UpperBoundError("one-render frozen policy mismatch")
    trace = report.get("execution_trace")
    if (
        not isinstance(trace, dict)
        or trace.get("independent_verify_commands") != 0
        or trace.get("target_render_commands") != TRIAL_COUNT
        or trace.get("warmup_render_commands") != 1
    ):
        raise UpperBoundError("one-render command trace mismatch")
    trials = report.get("trials")
    if not isinstance(trials, list) or len(trials) != TRIAL_COUNT:
        raise UpperBoundError("one-render trial set mismatch")
    for index, trial in enumerate(trials):
        if (
            not isinstance(trial, dict)
            or trial.get("trial_index") != index
            or trial.get("predeclared_for_quality") is not True
            or trial.get("pipeline", {}).get("publication", {}).get("authorization")
            is not False
            or trial.get("pipeline", {})
            .get("publication", {})
            .get("independent_gate_executed")
            is not False
        ):
            raise UpperBoundError("one-render trial semantics mismatch")
        wall = _finite_positive(trial.get("wall_s"), "trial wall")
        components = trial.get("components_s")
        expected_components = {
            "target_endpoint_and_manifest_s",
            "immutable_snapshot_conversion_s",
            "reconstruction_encode_validate_bind_s",
            "measurement_only_atomic_publication_s",
        }
        if not isinstance(components, dict) or set(components) != expected_components:
            raise UpperBoundError("one-render component timing shape mismatch")
        attributed = sum(
            _finite_positive(value, "component timing")
            for value in components.values()
        )
        unattributed = trial.get("unattributed_python_s")
        if (
            isinstance(unattributed, bool)
            or not isinstance(unattributed, (int, float))
            or not math.isfinite(float(unattributed))
            or float(unattributed) < 0.0
            or not math.isclose(
                wall,
                attributed + float(unattributed),
                rel_tol=0.0,
                abs_tol=5e-9,
            )
        ):
            raise UpperBoundError("one-render component timing does not close")
        artifacts = trial.get("artifacts")
        if not isinstance(artifacts, dict) or set(artifacts) != {
            "delivery",
            "draft",
            "draft_config",
            "draft_manifest",
        }:
            raise UpperBoundError("one-render trial artifact set mismatch")
        for label, artifact in artifacts.items():
            _resolve_record(output_root, artifact, f"trial {index} {label}")
    warmup = report.get("warmup")
    if (
        not isinstance(warmup, dict)
        or set(warmup)
        != {
            "artifacts",
            "charged",
            "pipeline",
            "target_endpoint_and_manifest_s",
            "target_render_invocations",
            "wall_s",
        }
        or warmup.get("charged") is not False
        or warmup.get("target_render_invocations") != 1
    ):
        raise UpperBoundError("one-render warmup scope mismatch")
    warm_artifacts = warmup.get("artifacts")
    if not isinstance(warm_artifacts, dict) or set(warm_artifacts) != {
        "delivery",
        "draft",
        "draft_manifest",
    }:
        raise UpperBoundError("one-render warmup artifact set mismatch")
    for label, artifact in warm_artifacts.items():
        _resolve_record(output_root, artifact, f"warmup {label}")
    _finite_positive(warmup.get("wall_s"), "warmup wall")
    _finite_positive(
        warmup.get("target_endpoint_and_manifest_s"),
        "warmup endpoint",
    )
    _validate_timing_block(report)
    finalization = report.get("finalization", {})
    if finalization != {
        "closed": False,
        "quality_v3_run": False,
        "quality_verifier_run": False,
    }:
        raise UpperBoundError("render report unexpectedly closed")
    canonical_json(report)


def validate_closed_report(report: dict[str, Any], output_root: Path) -> None:
    copy = dict(report)
    copy["finalization"] = {
        "closed": False,
        "quality_v3_run": False,
        "quality_verifier_run": False,
    }
    copy.pop("conclusion", None)
    copy.pop("budget_1000x", None)
    trials = []
    for trial in copy.get("trials", []):
        row = dict(trial)
        row.pop("quality", None)
        trials.append(row)
    copy["trials"] = trials
    validate_open_report(copy, output_root)
    finalization = report.get("finalization", {})
    if (
        not isinstance(finalization, dict)
        or set(finalization)
        != {
            "closed",
            "quality_and_verification_total_s",
            "quality_v3_run",
            "quality_verifier_run",
            "reference_used_for_candidate_selection",
        }
        or finalization.get("closed") is not True
        or finalization.get("quality_v3_run") is not True
        or finalization.get("quality_verifier_run") is not True
        or finalization.get("reference_used_for_candidate_selection") is not False
    ):
        raise UpperBoundError("one-render report is not closed")
    _finite_positive(
        finalization.get("quality_and_verification_total_s"),
        "quality finalization wall",
    )
    pass_count = 0
    reference = report.get("reference", {}).get("artifact", {})
    reference_path_value = reference.get("path")
    if not isinstance(reference_path_value, str):
        raise UpperBoundError("one-render reference artifact path mismatch")
    reference_path = Path(reference_path_value)
    if (
        not reference_path.is_absolute()
        or not reference_path.is_file()
        or reference_path.is_symlink()
        or reference_path.stat().st_size != reference.get("bytes")
        or sha256_file(reference_path) != reference.get("sha256")
    ):
        raise UpperBoundError("one-render reference artifact changed")
    for index, trial in enumerate(report["trials"]):
        quality = trial.get("quality", {})
        if not isinstance(quality, dict) or set(quality) != {
            "charged_to_candidate_wall",
            "independent_verification",
            "proof_artifact",
            "quality_v3_s",
            "result",
            "verification_artifact",
            "verification_s",
        }:
            raise UpperBoundError(f"trial {index} quality block shape mismatch")
        result = quality.get("result")
        verification = quality.get("independent_verification")
        candidate_record = trial["artifacts"]["delivery"]
        inputs = result.get("inputs") if isinstance(result, dict) else None
        candidate_input = (
            inputs.get("candidate") if isinstance(inputs, dict) else None
        )
        reference_input = (
            inputs.get("reference") if isinstance(inputs, dict) else None
        )
        verified_artifacts = (
            verification.get("artifacts")
            if isinstance(verification, dict)
            else None
        )
        if (
            not isinstance(result, dict)
            or not isinstance(verification, dict)
            or not isinstance(inputs, dict)
            or not isinstance(candidate_input, dict)
            or not isinstance(reference_input, dict)
            or not isinstance(verified_artifacts, dict)
            or quality.get("charged_to_candidate_wall") is not False
            or verification.get("proof_verified") is not True
            or verification.get("errors") != []
            or verification.get("quality_pass") is not result.get("pass")
            or verification.get("pass") is not result.get("pass")
            or verification.get("proof_result_sha256")
            != sha256_bytes(canonical_json(result))
            or verification.get("recomputed_result_sha256")
            != sha256_bytes(canonical_json(result))
            or inputs.get("target_dimensions") != list(OUTPUT_SIZE)
            or candidate_input.get("sha256") != candidate_record["sha256"]
            or candidate_input.get("bytes") != candidate_record["bytes"]
            or reference_input.get("sha256") != reference.get("sha256")
            or reference_input.get("bytes") != reference.get("bytes")
            or verified_artifacts.get("candidate")
            != {
                "bytes": candidate_record["bytes"],
                "sha256": candidate_record["sha256"],
            }
            or verified_artifacts.get("reference")
            != {
                "bytes": reference["bytes"],
                "sha256": reference["sha256"],
            }
        ):
            raise UpperBoundError(f"trial {index} quality verification mismatch")
        _finite_positive(quality.get("quality_v3_s"), "quality-v3 timing")
        _finite_positive(quality.get("verification_s"), "verification timing")
        proof_path = _resolve_record(
            output_root,
            quality["proof_artifact"],
            f"trial {index} proof",
        )
        verification_path = _resolve_record(
            output_root,
            quality["verification_artifact"],
            f"trial {index} quality verification",
        )
        stored_result = _read_json_object(proof_path, f"trial {index} proof")
        stored_verification = _read_json_object(
            verification_path,
            f"trial {index} verification",
        )
        if (
            canonical_json(stored_result) != canonical_json(result)
            or canonical_json(stored_verification) != canonical_json(verification)
        ):
            raise UpperBoundError(f"trial {index} stored quality evidence mismatch")
        candidate_path = _resolve_record(
            output_root,
            candidate_record,
            f"trial {index} delivery",
        )
        replayed = _recompute_quality_verification(
            proof_path,
            candidate_path,
            reference_path,
        )
        if canonical_json(replayed) != canonical_json(stored_verification):
            raise UpperBoundError(f"trial {index} independent quality replay mismatch")
        pass_count += int(result.get("pass") is True)
    if pass_count != _expected_conclusion(report)["quality_v3_pass_count"]:
        raise UpperBoundError("one-render quality pass count mismatch")
    if canonical_json(report.get("conclusion")) != canonical_json(
        _expected_conclusion(report)
    ):
        raise UpperBoundError("one-render conclusion mismatch")
    if canonical_json(report.get("budget_1000x")) != canonical_json(
        _expected_budget(report)
    ):
        raise UpperBoundError("one-render 1000x budget mismatch")
    canonical_json(report)


def finalize_report(report_path: Path) -> dict[str, Any]:
    if not report_path.is_absolute() or not report_path.is_file() or report_path.is_symlink():
        raise UpperBoundError("open report must be an absolute regular file")
    output_root = report_path.parent.resolve(strict=True)
    raw = report_path.read_bytes()
    report = _read_json_object(report_path, "open report")
    validate_open_report(report, output_root)
    _clear_incomplete_finalization_artifacts(output_root, report["trial_count"])
    starting_pins = _code_pins()
    if starting_pins != {
        key: value
        for key, value in report["pins"].items()
        if key != "blender_executable_sha256"
    }:
        raise UpperBoundError("pinned code changed before finalization")

    import cx_render_quality_v3 as quality
    import verify_cx_render_quality_v3 as verifier

    reference_record = report["reference"]["artifact"]
    reference = Path(reference_record["path"])
    if (
        not reference.is_absolute()
        or not reference.is_file()
        or reference.is_symlink()
        or reference.stat().st_size != reference_record["bytes"]
        or sha256_file(reference) != reference_record["sha256"]
    ):
        raise UpperBoundError("pinned reference changed before quality finalization")

    quality_total_started = time.perf_counter()
    for trial in report["trials"]:
        index = trial["trial_index"]
        delivery = _resolve_record(
            output_root, trial["artifacts"]["delivery"], f"trial {index} delivery"
        )
        quality_started = time.perf_counter()
        result = quality.evaluate_pngs(delivery, reference, target_size=OUTPUT_SIZE)
        quality_s = time.perf_counter() - quality_started
        proof_path = output_root / f"quality-v3-trial-{index:02d}.json"
        _write_json_atomic(proof_path, result, replace=False)
        verification_started = time.perf_counter()
        verification = verifier.verify_paths(proof_path, delivery, reference)
        verification_s = time.perf_counter() - verification_started
        if (
            verification.get("proof_verified") is not True
            or verification.get("quality_pass") is not result.get("pass")
            or verification.get("pass") is not result.get("pass")
            or verification.get("errors") != []
        ):
            raise UpperBoundError(f"trial {index} quality-v3 verification failed")
        verification_path = (
            output_root / f"quality-v3-verification-trial-{index:02d}.json"
        )
        _write_json_atomic(verification_path, verification, replace=False)
        trial["quality"] = {
            "charged_to_candidate_wall": False,
            "independent_verification": verification,
            "proof_artifact": _relative_record(proof_path, output_root),
            "quality_v3_s": _round9(quality_s),
            "result": result,
            "verification_artifact": _relative_record(
                verification_path, output_root
            ),
            "verification_s": _round9(verification_s),
        }
    quality_total_s = time.perf_counter() - quality_total_started

    report["timing"]["trial_wall_statistics"] = timing_statistics(
        [float(trial["wall_s"]) for trial in report["trials"]]
    )
    report["conclusion"] = _expected_conclusion(report)
    report["budget_1000x"] = _expected_budget(report)
    report["finalization"] = {
        "closed": True,
        "quality_and_verification_total_s": _round9(quality_total_s),
        "quality_v3_run": True,
        "quality_verifier_run": True,
        "reference_used_for_candidate_selection": False,
    }
    validate_closed_report(report, output_root)
    if _code_pins() != starting_pins:
        raise UpperBoundError("pinned code changed during finalization")
    _write_json_atomic(
        report_path,
        report,
        replace=True,
        expected_sha256=sha256_bytes(raw),
    )
    return report


def _cli_values(argv: Sequence[str]) -> list[str]:
    values = list(argv)
    if "--" in values:
        return values[values.index("--") + 1 :]
    return values[1:]


def main(argv: Sequence[str] | None = None) -> int:
    raw = sys.argv if argv is None else argv
    try:
        command, args = parse_outer(_cli_values(raw))
        if command == "render":
            report = render_screen(args)
            result = {
                "gpu_free": True,
                "kind": KIND,
                "ok": True,
                "report": str((args.output_root / REPORT_NAME).resolve()),
                "target_trials": report["trial_count"],
            }
        else:
            report = finalize_report(args.report)
            result = {
                "conclusion": report["conclusion"],
                "kind": KIND,
                "ok": True,
                "report": str(args.report.resolve()),
            }
        print(json.dumps(result, sort_keys=True, allow_nan=False), flush=True)
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error": f"{type(exc).__name__}: {exc}"[:1000],
                    "kind": f"{KIND}_error",
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
