#!/usr/bin/env python3
"""Audit-only screen for static-frame render artifact reuse.

The measured path starts before a strict cache-index lookup and ends only after
an exact cached artifact has been fully SHA-256 validated, durably published to
a new no-clobber path, and bound by a durable per-delivery receipt.  A final
screen receipt and all post-hoc quality evidence are deliberately outside that
headline and are disclosed as such.

This screen authorizes only the exact byte transport for an exactly bound cache
request.  It never upgrades the cached artifact: preview, production,
verification, and billing eligibility are inherited verbatim from the source
manifest.  General cross-frame reuse remains audit-only because equal serialized
scene bytes are not a fail-closed fingerprint of Blender's evaluated state.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import secrets
import stat
import statistics
import sys
import time
from typing import Any, Iterable, Sequence


SCHEMA_VERSION = 1
INDEX_KIND = "cx_static_frame_cache_index"
DELIVERY_KIND = "cx_static_frame_cache_delivery"
RECEIPT_KIND = "cx_static_frame_cache_reuse_screen"
QUALITY_KIND = "cx_render_quality_v3_result"
VERIFICATION_KIND = "cx_render_quality_v3_verification"
FRONTIER_KIND = "cx_spatial75_cycles_frontier"
MANIFEST_KIND = "cx_cycles_preview_manifest"
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
READ_SIZE = 1024 * 1024
DEFAULT_TRIALS = 9
DIMENSIONS = (1080, 1920)
REFERENCE_SAMPLES = 4096
SHA256_CHARS = frozenset("0123456789abcdef")
HERE = Path(__file__).resolve().parent
SCREEN_MODULE_PATH = Path(__file__).resolve()
SCREEN_TEST_PATH = HERE / "test_screen_static_frame_cache_reuse.py"
QUALITY_MODULE_PATH = HERE / "cx_render_quality_v3.py"
QUALITY_VERIFIER_PATH = HERE / "verify_cx_render_quality_v3.py"
QUALITY_V3_CONTRACT_SHA256 = (
    "6665e2931fed124108929bfd9cfc093c6db69407fcd7fc8a39644c4e39183b0b"
)


class CacheScreenError(RuntimeError):
    """A fail-closed cache screen error."""


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
        raise CacheScreenError("value is not canonical JSON") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in SHA256_CHARS for character in value)
    )


def _finite_positive(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise CacheScreenError(f"{label} must be finite and positive")
    return float(value)


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


def _content_identity(info: os.stat_result) -> tuple[int, ...]:
    """Identity fields unchanged by adding or removing a hard link."""

    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_size,
        info.st_mtime_ns,
    )


def _read_regular(
    path_value: str | Path,
    *,
    maximum: int,
    label: str,
    minimum: int = 1,
) -> tuple[bytes, os.stat_result]:
    path = Path(path_value)
    try:
        observed = path.lstat()
    except OSError as exc:
        raise CacheScreenError(f"{label} is unreadable") from exc
    if path.is_symlink() or not stat.S_ISREG(observed.st_mode):
        raise CacheScreenError(f"{label} is not a regular non-symlink file")
    if not minimum <= observed.st_size <= maximum:
        raise CacheScreenError(f"{label} size is invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CacheScreenError(f"{label} is unreadable") from exc
    chunks: list[bytes] = []
    total = 0
    try:
        before = os.fstat(descriptor)
        if _identity(before) != _identity(observed):
            raise CacheScreenError(f"{label} changed before read")
        while True:
            chunk = os.read(descriptor, min(READ_SIZE, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise CacheScreenError(f"{label} size is invalid")
        after = os.fstat(descriptor)
    except OSError as exc:
        raise CacheScreenError(f"{label} is unreadable") from exc
    finally:
        os.close(descriptor)
    try:
        current = path.lstat()
    except OSError as exc:
        raise CacheScreenError(f"{label} changed during read") from exc
    if (
        _identity(before) != _identity(after)
        or _identity(after) != _identity(current)
        or total != after.st_size
    ):
        raise CacheScreenError(f"{label} changed during read")
    return b"".join(chunks), after


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CacheScreenError("JSON contains a duplicate key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise CacheScreenError("JSON contains a non-finite number")


def _parse_json(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except CacheScreenError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise CacheScreenError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise CacheScreenError(f"{label} must be a JSON object")
    return value


def _read_json(path: str | Path, label: str) -> tuple[dict[str, Any], bytes]:
    data, _ = _read_regular(path, maximum=MAX_JSON_BYTES, label=label)
    return _parse_json(data, label), data


def implementation_pins() -> dict[str, str]:
    records = {}
    for label, path in (
        ("screen_module_sha256", SCREEN_MODULE_PATH),
        ("screen_test_sha256", SCREEN_TEST_PATH),
        ("quality_v3_module_sha256", QUALITY_MODULE_PATH),
        ("quality_v3_verifier_sha256", QUALITY_VERIFIER_PATH),
    ):
        data, _ = _read_regular(path, maximum=MAX_JSON_BYTES, label=label)
        records[label] = sha256_bytes(data)
    records["quality_v3_contract_sha256"] = QUALITY_V3_CONTRACT_SHA256
    return records


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise CacheScreenError("short write while publishing")
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_real_directory(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise CacheScreenError(f"{label} is unavailable") from exc
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise CacheScreenError(f"{label} is not a real directory")


def _publish_bytes_new(path: Path, data: bytes) -> int:
    """Durably publish exact bytes through a retained, verified stage FD."""

    started = time.perf_counter_ns()
    if not isinstance(data, bytes):
        raise CacheScreenError("publication payload must be bytes")
    expected_bytes = len(data)
    expected_sha256 = sha256_bytes(data)
    _require_real_directory(path.parent, "publication parent")
    if path.exists() or path.is_symlink():
        raise CacheScreenError("publication destination already exists")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(16)}.tmp")
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    published_fd: int | None = None
    destination_linked = False
    publication_complete = False
    try:
        descriptor = os.open(temporary, flags, 0o600)
        _write_all(descriptor, data)
        os.fsync(descriptor)
        stage_before_link = os.fstat(descriptor)
        if (
            not stat.S_ISREG(stage_before_link.st_mode)
            or stage_before_link.st_size != expected_bytes
        ):
            raise CacheScreenError("publication stage identity is invalid")
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise CacheScreenError("publication destination already exists") from exc
        destination_linked = True

        destination_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        destination_flags |= getattr(os, "O_NOFOLLOW", 0)
        published_fd = os.open(path, destination_flags)
        published_before = os.fstat(published_fd)
        stage_after_link = os.fstat(descriptor)
        if (
            _content_identity(stage_after_link)
            != _content_identity(stage_before_link)
            or published_before.st_dev != stage_after_link.st_dev
            or published_before.st_ino != stage_after_link.st_ino
            or published_before.st_size != expected_bytes
            or not stat.S_ISREG(published_before.st_mode)
        ):
            raise CacheScreenError("published byte stage identity mismatch")

        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(published_fd, READ_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if total > expected_bytes:
                raise CacheScreenError("published byte count mismatch")
        published_after = os.fstat(published_fd)
        current = path.lstat()
        if (
            _identity(published_before) != _identity(published_after)
            or _identity(published_after) != _identity(current)
            or total != expected_bytes
            or digest.hexdigest() != expected_sha256
        ):
            raise CacheScreenError("published bytes full SHA-256 mismatch")
        os.fsync(published_fd)

        temporary.unlink()
        _fsync_directory(path.parent)
        os.lseek(published_fd, 0, os.SEEK_SET)
        final_before = os.fstat(published_fd)
        final_digest = hashlib.sha256()
        final_total = 0
        while True:
            chunk = os.read(published_fd, READ_SIZE)
            if not chunk:
                break
            final_digest.update(chunk)
            final_total += len(chunk)
            if final_total > expected_bytes:
                raise CacheScreenError("final published byte count mismatch")
        final_after = os.fstat(published_fd)
        final_path = path.lstat()
        if (
            _identity(final_before) != _identity(final_after)
            or _identity(final_after) != _identity(final_path)
            or final_total != expected_bytes
            or final_digest.hexdigest() != expected_sha256
        ):
            raise CacheScreenError("final published bytes identity mismatch")
        os.fsync(published_fd)
        publication_complete = True
    except OSError as exc:
        raise CacheScreenError("durable byte publication failed") from exc
    finally:
        if published_fd is not None:
            os.close(published_fd)
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        if destination_linked and not publication_complete:
            path.unlink(missing_ok=True)
            try:
                _fsync_directory(path.parent)
            except OSError:
                pass
    return time.perf_counter_ns() - started


def _open_and_hash_source(
    path: Path, *, expected_sha256: str, expected_bytes: int
) -> tuple[int, os.stat_result, int]:
    """Open once, hash every byte, and retain the validated descriptor."""

    started = time.perf_counter_ns()
    if not _is_sha256(expected_sha256) or not 1 <= expected_bytes <= MAX_ARTIFACT_BYTES:
        raise CacheScreenError("cache artifact identity is invalid")
    try:
        observed = path.lstat()
    except OSError as exc:
        raise CacheScreenError("cached artifact is unreadable") from exc
    if path.is_symlink() or not stat.S_ISREG(observed.st_mode):
        raise CacheScreenError("cached artifact is not a regular non-symlink file")
    if observed.st_size != expected_bytes:
        raise CacheScreenError("cached artifact byte count mismatch")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if _identity(before) != _identity(observed):
            raise CacheScreenError("cached artifact changed before validation")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, READ_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if total > expected_bytes:
                raise CacheScreenError("cached artifact byte count mismatch")
        after = os.fstat(descriptor)
        current = path.lstat()
        if (
            _identity(before) != _identity(after)
            or _identity(after) != _identity(current)
            or total != expected_bytes
            or digest.hexdigest() != expected_sha256
        ):
            raise CacheScreenError("cached artifact full SHA-256 validation failed")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor, after, time.perf_counter_ns() - started
    except BaseException:
        os.close(descriptor)
        raise


def _new_temporary_path(destination: Path) -> Path:
    for _ in range(32):
        candidate = destination.with_name(
            f".{destination.name}.{secrets.token_hex(16)}.stage"
        )
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise CacheScreenError("could not allocate publication staging path")


def _publish_validated_descriptor(
    source_path: Path,
    source_fd: int,
    source_info: os.stat_result,
    destination: Path,
    *,
    expected_sha256: str,
    expected_bytes: int,
    prefer_hardlink: bool = True,
) -> tuple[str, int, dict[str, Any]]:
    """Publish through a retained stage FD and verify the exposed inode/bytes."""

    started = time.perf_counter_ns()
    _require_real_directory(destination.parent, "delivery parent")
    if destination.exists() or destination.is_symlink():
        raise CacheScreenError("delivery destination already exists")
    temporary = _new_temporary_path(destination)
    mode = "hardlink"
    temporary_created = False
    stage_fd: int | None = None
    published_fd: int | None = None
    destination_linked = False
    publication_complete = False
    published_identity: dict[str, Any] | None = None
    try:
        if prefer_hardlink:
            try:
                current = source_path.lstat()
                if _content_identity(current) != _content_identity(source_info):
                    raise CacheScreenError("cached artifact changed before publication")
                os.link(source_path, temporary, follow_symlinks=False)
                temporary_created = True
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                stage_fd = os.open(temporary, flags)
                linked_info = os.fstat(stage_fd)
                if _content_identity(linked_info) != _content_identity(source_info):
                    raise CacheScreenError("hard-link source identity mismatch")
                os.fsync(stage_fd)
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                mode = "copy"
        else:
            mode = "copy"
        if mode == "copy":
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            stage_fd = os.open(temporary, flags, 0o600)
            temporary_created = True
            digest = hashlib.sha256()
            total = 0
            os.lseek(source_fd, 0, os.SEEK_SET)
            while True:
                chunk = os.read(source_fd, READ_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
                _write_all(stage_fd, chunk)
            os.fsync(stage_fd)
            after_copy = os.fstat(source_fd)
            if (
                _content_identity(after_copy) != _content_identity(source_info)
                or total != expected_bytes
                or digest.hexdigest() != expected_sha256
            ):
                raise CacheScreenError("cached artifact changed during copy")
        if stage_fd is None:
            raise CacheScreenError("publication stage descriptor is unavailable")
        stage_before_link = os.fstat(stage_fd)
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError as exc:
            raise CacheScreenError("delivery destination already exists") from exc
        destination_linked = True
        destination_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        destination_flags |= getattr(os, "O_NOFOLLOW", 0)
        published_fd = os.open(destination, destination_flags)
        published_before = os.fstat(published_fd)
        stage_after_link = os.fstat(stage_fd)
        if (
            _content_identity(stage_after_link)
            != _content_identity(stage_before_link)
            or published_before.st_dev != stage_after_link.st_dev
            or published_before.st_ino != stage_after_link.st_ino
            or published_before.st_size != expected_bytes
            or not stat.S_ISREG(published_before.st_mode)
        ):
            raise CacheScreenError("published delivery stage identity mismatch")
        published_digest = hashlib.sha256()
        published_total = 0
        while True:
            chunk = os.read(published_fd, READ_SIZE)
            if not chunk:
                break
            published_digest.update(chunk)
            published_total += len(chunk)
            if published_total > expected_bytes:
                raise CacheScreenError("published delivery byte count mismatch")
        published_after = os.fstat(published_fd)
        current = destination.lstat()
        if (
            _identity(published_before) != _identity(published_after)
            or _identity(published_after) != _identity(current)
            or published_total != expected_bytes
            or published_digest.hexdigest() != expected_sha256
        ):
            raise CacheScreenError("published delivery full SHA-256 mismatch")
        os.fsync(published_fd)
        temporary.unlink()
        temporary_created = False
        _fsync_directory(destination.parent)

        # Retain the destination descriptor through the parent-directory
        # durability boundary, then rebind the exposed name and every byte to
        # that exact open inode.  This rejects a same-UID path substitution
        # during directory fsync instead of relying on the storage trust note.
        os.lseek(published_fd, 0, os.SEEK_SET)
        final_before = os.fstat(published_fd)
        final_digest = hashlib.sha256()
        final_total = 0
        while True:
            chunk = os.read(published_fd, READ_SIZE)
            if not chunk:
                break
            final_digest.update(chunk)
            final_total += len(chunk)
            if final_total > expected_bytes:
                raise CacheScreenError("final published delivery byte count mismatch")
        final_after = os.fstat(published_fd)
        final_path = destination.lstat()
        if (
            _identity(final_before) != _identity(final_after)
            or _identity(final_after) != _identity(final_path)
            or final_total != expected_bytes
            or final_digest.hexdigest() != expected_sha256
        ):
            raise CacheScreenError("final published delivery identity mismatch")
        os.fsync(published_fd)
        published_identity = {
            "bytes": final_total,
            "sha256": final_digest.hexdigest(),
        }
        publication_complete = True
    except OSError as exc:
        raise CacheScreenError("durable delivery publication failed") from exc
    finally:
        if published_fd is not None:
            os.close(published_fd)
        if stage_fd is not None:
            os.close(stage_fd)
        if temporary_created:
            temporary.unlink(missing_ok=True)
        if destination_linked and not publication_complete:
            destination.unlink(missing_ok=True)
            try:
                _fsync_directory(destination.parent)
            except OSError:
                pass
    if published_identity is None:
        raise CacheScreenError("published delivery identity is unavailable")
    return mode, time.perf_counter_ns() - started, published_identity


def _resolve_beneath(root: Path, relative: str, label: str) -> Path:
    relative_path = Path(relative) if isinstance(relative, str) else Path()
    if (
        not isinstance(relative, str)
        or not relative
        or relative_path.is_absolute()
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        raise CacheScreenError(f"{label} path is not relative")
    root_resolved = root.resolve(strict=True)
    _require_real_directory(root_resolved, f"{label} artifact root")
    current = root_resolved
    for index, part in enumerate(relative_path.parts):
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise CacheScreenError(f"{label} is unavailable") from exc
        if stat.S_ISLNK(info.st_mode):
            raise CacheScreenError(f"{label} path contains a symlink")
        if index < len(relative_path.parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise CacheScreenError(f"{label} parent is not a directory")
    candidate = current.resolve(strict=True)
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise CacheScreenError(f"{label} escapes its artifact root") from exc
    return candidate


def _relative_record(path: Path, root: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise CacheScreenError("artifact is outside the screen root") from exc
    data, info = _read_regular(
        resolved, maximum=MAX_ARTIFACT_BYTES, label="screen artifact"
    )
    return {
        "path": relative.as_posix(),
        "bytes": info.st_size,
        "sha256": sha256_bytes(data),
    }


def _type7_quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise CacheScreenError("quantile requires samples")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _cache_key_payload(request_identity: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "cx_static_frame_exact_request_key_v1",
        "request_identity": request_identity,
    }


def request_identity_from_manifest(value: dict[str, Any]) -> dict[str, Any]:
    """Project the exact render request separately from its cached artifact."""

    try:
        render = value["render"]
        scene = value["scene"]
        pins = value["pins"]
        artifact = value["artifact"]
    except (KeyError, TypeError) as exc:
        raise CacheScreenError("manifest request identity is incomplete") from exc
    required_pins = {
        "backend_sha256",
        "blender_sha256",
        "child_script_sha256",
        "controller_adapter_sha256",
        "controller_core_sha256",
    }
    if (
        not _is_sha256(value.get("binding_sha256"))
        or not isinstance(pins, dict)
        or not required_pins.issubset(pins)
        or any(not _is_sha256(pins.get(key)) for key in required_pins)
        or render.get("frame") != 9
        or render.get("samples") != REFERENCE_SAMPLES
        or render.get("width") != DIMENSIONS[0]
        or render.get("height") != DIMENSIONS[1]
        or artifact.get("media_type") != "image/png"
    ):
        raise CacheScreenError("manifest exact request identity mismatch")
    return {
        "schema_version": 1,
        "kind": "cx_static_frame_exact_request_identity",
        "manifest_binding_sha256": value["binding_sha256"],
        "unit_id": value.get("unit_id"),
        "phase": value.get("phase"),
        "frame": render["frame"],
        "scene_bundle": {
            "bytes": scene.get("bundle_bytes"),
            "files": scene.get("bundle_files"),
            "sha256": scene.get("bundle_sha256"),
            "main_blend_sha256": scene.get("sha256"),
            "relative_path": scene.get("relative_path"),
        },
        "render_recipe_and_policy": render,
        "runtime_and_implementation_pins": {
            key: pins[key] for key in sorted(required_pins)
        },
        "expected_output_contract": {
            "media_type": artifact["media_type"],
            "width": render["width"],
            "height": render["height"],
            "mode": "RGBA",
            "png_compression": render.get("png_compression"),
            "samples": render["samples"],
            "engine": render.get("engine"),
            "device": render.get("device"),
            "preview_only": value.get("preview_only"),
            "production_ready": value.get("production_ready"),
            "artifact_verified": value.get("artifact_verified"),
            "billing_eligible": value.get("billing_eligible"),
        },
    }


def source_eligibility_from_manifest(value: dict[str, Any]) -> dict[str, bool]:
    eligibility = {
        "preview_only": value.get("preview_only"),
        "production_ready": value.get("production_ready"),
        "artifact_verified": value.get("artifact_verified"),
        "billing_eligible": value.get("billing_eligible"),
    }
    if any(type(entry) is not bool for entry in eligibility.values()):
        raise CacheScreenError("source artifact eligibility is incomplete")
    return eligibility


def build_cache_index(
    *,
    artifact: dict[str, Any],
    source_manifest: dict[str, Any],
    serialized_scene_identity: dict[str, Any],
    request_identity: dict[str, Any],
) -> dict[str, Any]:
    key = sha256_bytes(canonical_json(_cache_key_payload(request_identity)))
    value = {
        "schema_version": SCHEMA_VERSION,
        "kind": INDEX_KIND,
        "cache_key": key,
        "authorization": {
            "exact_transport_authorized": True,
            "cross_frame_reuse_authorized": False,
            "scope_partitioned": True,
            "source_artifact_eligibility_inherited": True,
        },
        "artifact": artifact,
        "request_identity": request_identity,
        "source_manifest": source_manifest,
        "serialized_scene_identity": serialized_scene_identity,
    }
    validate_cache_index(value, expected_key=key)
    return value


def validate_cache_index(value: Any, *, expected_key: str) -> None:
    if not isinstance(value, dict) or set(value) != {
        "artifact",
        "authorization",
        "cache_key",
        "kind",
        "request_identity",
        "schema_version",
        "serialized_scene_identity",
        "source_manifest",
    }:
        raise CacheScreenError("cache index shape mismatch")
    if (
        value["schema_version"] != SCHEMA_VERSION
        or value["kind"] != INDEX_KIND
        or value["cache_key"] != expected_key
        or not _is_sha256(expected_key)
        or value["authorization"]
        != {
            "exact_transport_authorized": True,
            "cross_frame_reuse_authorized": False,
            "scope_partitioned": True,
            "source_artifact_eligibility_inherited": True,
        }
    ):
        raise CacheScreenError("cache index identity mismatch")
    artifact = value["artifact"]
    manifest = value["source_manifest"]
    scene = value["serialized_scene_identity"]
    request_identity = value["request_identity"]
    if not isinstance(artifact, dict) or set(artifact) != {
        "bytes",
        "dimensions",
        "mode",
        "path",
        "samples",
        "sha256",
        "source_frame",
    }:
        raise CacheScreenError("cache artifact record shape mismatch")
    if (
        not isinstance(artifact["path"], str)
        or Path(artifact["path"]).is_absolute()
        or not 1 <= artifact["bytes"] <= MAX_ARTIFACT_BYTES
        or not _is_sha256(artifact["sha256"])
        or artifact["dimensions"] != list(DIMENSIONS)
        or artifact["mode"] != "RGBA"
        or artifact["samples"] != REFERENCE_SAMPLES
        or artifact["source_frame"] != 9
    ):
        raise CacheScreenError("cache artifact identity mismatch")
    if not isinstance(manifest, dict) or set(manifest) != {
        "bytes",
        "path",
        "sha256",
    }:
        raise CacheScreenError("source manifest record shape mismatch")
    if (
        not isinstance(manifest["path"], str)
        or Path(manifest["path"]).is_absolute()
        or not 1 <= manifest["bytes"] <= MAX_JSON_BYTES
        or not _is_sha256(manifest["sha256"])
    ):
        raise CacheScreenError("source manifest identity mismatch")
    if not isinstance(scene, dict) or set(scene) != {
        "bundle_bytes",
        "bundle_files",
        "bundle_sha256",
        "main_blend_sha256",
    }:
        raise CacheScreenError("serialized scene identity shape mismatch")
    if (
        type(scene["bundle_bytes"]) is not int
        or scene["bundle_bytes"] <= 0
        or type(scene["bundle_files"]) is not int
        or scene["bundle_files"] <= 0
        or not _is_sha256(scene["bundle_sha256"])
        or not _is_sha256(scene["main_blend_sha256"])
    ):
        raise CacheScreenError("serialized scene identity mismatch")
    expected_request_keys = {
        "expected_output_contract",
        "frame",
        "kind",
        "manifest_binding_sha256",
        "phase",
        "render_recipe_and_policy",
        "runtime_and_implementation_pins",
        "scene_bundle",
        "schema_version",
        "unit_id",
    }
    expected_pin_keys = {
        "backend_sha256",
        "blender_sha256",
        "child_script_sha256",
        "controller_adapter_sha256",
        "controller_core_sha256",
    }
    expected_output_keys = {
        "artifact_verified",
        "billing_eligible",
        "device",
        "engine",
        "height",
        "media_type",
        "mode",
        "png_compression",
        "preview_only",
        "production_ready",
        "samples",
        "width",
    }
    if (
        not isinstance(request_identity, dict)
        or set(request_identity) != expected_request_keys
        or request_identity.get("kind")
        != "cx_static_frame_exact_request_identity"
        or request_identity.get("schema_version") != 1
        or not isinstance(request_identity.get("unit_id"), str)
        or not request_identity["unit_id"]
        or request_identity.get("phase") != "baseline"
        or request_identity.get("frame") != 9
        or not _is_sha256(request_identity.get("manifest_binding_sha256"))
        or request_identity.get("scene_bundle")
        != {
            "bytes": scene["bundle_bytes"],
            "files": scene["bundle_files"],
            "sha256": scene["bundle_sha256"],
            "main_blend_sha256": scene["main_blend_sha256"],
            "relative_path": request_identity.get("scene_bundle", {}).get(
                "relative_path"
            ),
        }
        or not isinstance(
            request_identity.get("scene_bundle", {}).get("relative_path"), str
        )
        or not request_identity["scene_bundle"]["relative_path"]
        or request_identity.get("expected_output_contract", {}).get("mode")
        != artifact["mode"]
        or request_identity.get("expected_output_contract", {}).get("width")
        != artifact["dimensions"][0]
        or request_identity.get("expected_output_contract", {}).get("height")
        != artifact["dimensions"][1]
        or request_identity.get("expected_output_contract", {}).get("samples")
        != artifact["samples"]
        or not isinstance(
            request_identity.get("runtime_and_implementation_pins"), dict
        )
        or set(request_identity["runtime_and_implementation_pins"])
        != expected_pin_keys
        or any(
            not _is_sha256(value)
            for value in request_identity.get(
                "runtime_and_implementation_pins", {}
            ).values()
        )
        or not isinstance(request_identity.get("render_recipe_and_policy"), dict)
        or request_identity["render_recipe_and_policy"].get("frame") != 9
        or request_identity["render_recipe_and_policy"].get("samples")
        != REFERENCE_SAMPLES
        or request_identity["render_recipe_and_policy"].get("width")
        != DIMENSIONS[0]
        or request_identity["render_recipe_and_policy"].get("height")
        != DIMENSIONS[1]
        or not isinstance(
            request_identity["render_recipe_and_policy"].get(
                "worker_renderer_identity"
            ),
            dict,
        )
        or set(request_identity.get("expected_output_contract", {}))
        != expected_output_keys
        or request_identity["expected_output_contract"].get("media_type")
        != "image/png"
        or any(
            type(request_identity["expected_output_contract"].get(key)) is not bool
            for key in (
                "preview_only",
                "production_ready",
                "artifact_verified",
                "billing_eligible",
            )
        )
    ):
        raise CacheScreenError("exact request identity mismatch")
    recomputed = sha256_bytes(canonical_json(_cache_key_payload(request_identity)))
    if recomputed != expected_key:
        raise CacheScreenError("cache key binding mismatch")


def _request_scene_projection(request_identity: dict[str, Any]) -> dict[str, Any]:
    """Return the serialized-scene tuple committed by the exact request."""

    try:
        scene = request_identity["scene_bundle"]
        projection = {
            "bundle_bytes": scene["bytes"],
            "bundle_files": scene["files"],
            "bundle_sha256": scene["sha256"],
            "main_blend_sha256": scene["main_blend_sha256"],
        }
    except (KeyError, TypeError) as exc:
        raise CacheScreenError("exact request scene projection is incomplete") from exc
    if (
        type(projection["bundle_bytes"]) is not int
        or projection["bundle_bytes"] <= 0
        or type(projection["bundle_files"]) is not int
        or projection["bundle_files"] <= 0
        or not _is_sha256(projection["bundle_sha256"])
        or not _is_sha256(projection["main_blend_sha256"])
    ):
        raise CacheScreenError("exact request scene projection is invalid")
    return projection


def _validate_aggregate_cache_source_tuple(
    *,
    index: dict[str, Any],
    source: dict[str, Any],
    cache_source_record: dict[str, Any],
    source_manifest_record: dict[str, Any],
    cache_source_basename: str,
    source_manifest_basename: str,
    manifest_scene: dict[str, Any],
) -> None:
    """Cross-bind every cache source identity represented by the receipt.

    The index paths are relative to the cache directory while aggregate artifact
    paths are relative to the screen root, so their basenames are bound alongside
    every content and semantic field rather than comparing unlike path strings.
    """

    source_artifact = source.get("artifact")
    request_identity = source.get("request_identity")
    source_scene = source.get("serialized_scene_identity")
    if (
        not isinstance(source_artifact, dict)
        or set(source_artifact) != {"bytes", "path", "sha256"}
        or not isinstance(cache_source_record, dict)
        or set(cache_source_record) != {"bytes", "path", "sha256"}
        or not isinstance(source_manifest_record, dict)
        or set(source_manifest_record) != {"bytes", "path", "sha256"}
        or not isinstance(request_identity, dict)
        or not isinstance(source_scene, dict)
    ):
        raise CacheScreenError("aggregate cache source tuple is incomplete")
    if cache_source_record != source_artifact:
        raise CacheScreenError("source artifact table binding mismatch")
    if (
        Path(cache_source_record["path"]).name != cache_source_basename
        or Path(source_manifest_record.get("path", "")).name
        != source_manifest_basename
    ):
        raise CacheScreenError("cache tuple basename binding mismatch")
    try:
        expected_index_artifact = {
            "path": Path(cache_source_record["path"]).name,
            "bytes": source_artifact["bytes"],
            "sha256": source_artifact["sha256"],
            "dimensions": source["dimensions"],
            "mode": request_identity["expected_output_contract"]["mode"],
            "samples": source["samples"],
            "source_frame": source["frame"],
        }
        expected_index_manifest = {
            "path": Path(source_manifest_record["path"]).name,
            "bytes": source_manifest_record["bytes"],
            "sha256": source_manifest_record["sha256"],
        }
    except (KeyError, TypeError) as exc:
        raise CacheScreenError("aggregate cache source tuple is incomplete") from exc
    if index.get("artifact") != expected_index_artifact:
        raise CacheScreenError("cache index artifact tuple binding mismatch")
    if index.get("source_manifest") != expected_index_manifest:
        raise CacheScreenError("cache index source manifest tuple binding mismatch")
    request_scene = _request_scene_projection(request_identity)
    if not (
        index.get("serialized_scene_identity")
        == source_scene
        == manifest_scene
        == request_scene
    ):
        raise CacheScreenError("cache serialized scene tuple binding mismatch")


def _validate_source_performance_binding(
    *,
    performance: dict[str, Any],
    source: dict[str, Any],
    source_manifest: dict[str, Any],
    exact_baseline: float,
) -> None:
    """Bind the exact-transport denominator to its source render artifact."""

    request_identity = source["request_identity"]
    request_recipe = request_identity["render_recipe_and_policy"]
    request_output = request_identity["expected_output_contract"]
    request_pins = request_identity["runtime_and_implementation_pins"]
    audit = performance.get("benchmark_audit")
    baseline = audit.get("baseline") if isinstance(audit, dict) else None
    performance_pins = performance.get("pins")
    manifest_artifact = source_manifest.get("artifact")
    manifest_scene = source_manifest.get("scene")
    manifest_render = source_manifest.get("render")
    common_pin_keys = {
        "backend_sha256",
        "blender_sha256",
        "controller_adapter_sha256",
        "controller_core_sha256",
    }
    if (
        not isinstance(audit, dict)
        or not isinstance(baseline, dict)
        or not isinstance(performance_pins, dict)
        or not isinstance(manifest_artifact, dict)
        or not isinstance(manifest_scene, dict)
        or not isinstance(manifest_render, dict)
    ):
        raise CacheScreenError("source performance receipt binding is incomplete")
    try:
        performance_manifest_path = Path(baseline["manifest_path"])
        manifest_artifact_path = Path(manifest_artifact["path"])
    except (KeyError, TypeError) as exc:
        raise CacheScreenError("source performance receipt binding is incomplete") from exc
    if (
        performance.get("schema_version") != 1
        or performance.get("kind") != "cx_local_cycles_spec_benchmark"
        or performance.get("evidence") != "measured"
        or performance.get("cache_used") is not False
        or performance.get("frame") != source["frame"]
        or performance.get("reference_samples") != source["samples"]
        or performance.get("resolution") != source["dimensions"]
        or performance.get("device") != request_output["device"]
        or performance.get("device") != request_recipe.get("device")
        or performance.get("scene_sha256")
        != source["serialized_scene_identity"]["main_blend_sha256"]
        or performance.get("scene_sha256") != manifest_scene.get("sha256")
        or Path(performance.get("scene", "")).name
        != request_identity["scene_bundle"]["relative_path"]
        or not math.isclose(
            _finite_positive(
                performance.get("baseline_s"), "source performance baseline"
            ),
            exact_baseline,
            rel_tol=0.0,
            abs_tol=0.0,
        )
        or performance.get("preview_only")
        is not source["eligibility"]["preview_only"]
        or performance.get("production_ready")
        is not source["eligibility"]["production_ready"]
        or performance.get("receipt_trust") != "local_unattested"
        or audit.get("kind") != "cx_cycles_preview_benchmark_audit"
        or audit.get("schema_version") != 2
        or audit.get("measurement_only") is not True
        or baseline.get("phase") != "baseline"
        or baseline.get("artifact_sha256") != source["artifact"]["sha256"]
        or baseline.get("artifact_sha256") != manifest_artifact.get("sha256")
        or baseline.get("sample_offset") != request_recipe.get("sample_offset")
        or audit.get("binding_sha256")
        != request_identity["manifest_binding_sha256"]
        or audit.get("binding_sha256") != source_manifest.get("binding_sha256")
        or performance_manifest_path.name != "baseline-manifest.json"
        or manifest_artifact_path.name != "baseline.png"
        or performance_manifest_path.parent != manifest_artifact_path.parent
        or performance.get("worker_renderer_identity")
        != request_recipe.get("worker_renderer_identity")
        or performance.get("worker_renderer_identity")
        != manifest_render.get("worker_renderer_identity")
        or any(
            performance_pins.get(key) != request_pins.get(key)
            or performance_pins.get(key) != source_manifest.get("pins", {}).get(key)
            for key in common_pin_keys
        )
    ):
        raise CacheScreenError("source performance denominator binding mismatch")


def _lookup_cache_index(
    index_path: Path, *, expected_sha256: str, expected_key: str
) -> tuple[dict[str, Any], Path, int]:
    started = time.perf_counter_ns()
    data, _ = _read_regular(
        index_path, maximum=MAX_JSON_BYTES, label="cache index"
    )
    if sha256_bytes(data) != expected_sha256:
        raise CacheScreenError("cache index SHA-256 mismatch")
    value = _parse_json(data, "cache index")
    validate_cache_index(value, expected_key=expected_key)
    source_path = _resolve_beneath(
        index_path.parent, value["artifact"]["path"], "cached artifact"
    )
    return value, source_path, time.perf_counter_ns() - started


def _delivery_sidecar(
    *,
    trial_index: int,
    cache_index_sha256: str,
    cache_key: str,
    artifact: dict[str, Any],
    output_path: str,
    publication_mode: str,
    lookup_ns: int,
    validation_ns: int,
    publication_ns: int,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": DELIVERY_KIND,
        "trial_index": trial_index,
        "authorization": {
            "exact_transport_authorized": True,
            "cross_frame_reuse": False,
            "source_artifact_eligibility_inherited": True,
        },
        "cache_index_sha256": cache_index_sha256,
        "cache_key": cache_key,
        "source": {
            "bytes": artifact["bytes"],
            "sha256": artifact["sha256"],
            "source_frame": artifact["source_frame"],
            "samples": artifact["samples"],
        },
        "output": {
            "path": output_path,
            "bytes": artifact["bytes"],
            "sha256": artifact["sha256"],
        },
        "publication_mode": publication_mode,
        "timing_scope": (
            "cache index lookup, full source SHA-256 validation, and durable "
            "no-clobber artifact publication; durable sidecar publication is "
            "measured externally by the enclosing screen"
        ),
        "completed_stage_ns": {
            "cache_lookup": lookup_ns,
            "full_sha256_validation": validation_ns,
            "durable_artifact_publication": publication_ns,
        },
    }


def run_timed_trial(
    *,
    index_path: Path,
    index_sha256: str,
    cache_key: str,
    trials_root: Path,
    screen_root: Path,
    trial_index: int,
    prefer_hardlink: bool = True,
) -> dict[str, Any]:
    """Run one closed cache transaction and return its post-timing record."""

    if trial_index < 0:
        raise CacheScreenError("trial index is invalid")
    _require_real_directory(trials_root, "trials root")
    delivery = trials_root / f"trial-{trial_index:02d}-delivery.png"
    sidecar_path = trials_root / f"trial-{trial_index:02d}-receipt.json"
    if (
        delivery.exists()
        or delivery.is_symlink()
        or sidecar_path.exists()
        or sidecar_path.is_symlink()
    ):
        raise CacheScreenError("trial output already exists")

    total_started = time.perf_counter_ns()
    index, source_path, lookup_ns = _lookup_cache_index(
        index_path,
        expected_sha256=index_sha256,
        expected_key=cache_key,
    )
    artifact = index["artifact"]
    source_fd, source_info, validation_ns = _open_and_hash_source(
        source_path,
        expected_sha256=artifact["sha256"],
        expected_bytes=artifact["bytes"],
    )
    try:
        publication_mode, publication_ns, published_identity = (
            _publish_validated_descriptor(
                source_path,
                source_fd,
                source_info,
                delivery,
                expected_sha256=artifact["sha256"],
                expected_bytes=artifact["bytes"],
                prefer_hardlink=prefer_hardlink,
            )
        )
    finally:
        os.close(source_fd)
    if published_identity != {
        "bytes": artifact["bytes"],
        "sha256": artifact["sha256"],
    }:
        delivery.unlink(missing_ok=True)
        _fsync_directory(delivery.parent)
        raise CacheScreenError("post-publication delivery identity mismatch")
    receipt_started = time.perf_counter_ns()
    sidecar = _delivery_sidecar(
        trial_index=trial_index,
        cache_index_sha256=index_sha256,
        cache_key=cache_key,
        artifact=artifact,
        output_path=delivery.relative_to(screen_root).as_posix(),
        publication_mode=publication_mode,
        lookup_ns=lookup_ns,
        validation_ns=validation_ns,
        publication_ns=publication_ns,
    )
    try:
        _publish_bytes_new(sidecar_path, canonical_json(sidecar) + b"\n")
    except BaseException:
        try:
            delivery.unlink(missing_ok=True)
            _fsync_directory(delivery.parent)
        except OSError as cleanup_error:
            raise CacheScreenError(
                "sidecar publication failed and delivery rollback failed"
            ) from cleanup_error
        raise
    receipt_ns = time.perf_counter_ns() - receipt_started
    total_ns = time.perf_counter_ns() - total_started
    return {
        "trial_index": trial_index,
        "total_s": total_ns / 1_000_000_000,
        "timings_ns": {
            "cache_lookup": lookup_ns,
            "full_sha256_validation": validation_ns,
            "durable_artifact_publication": publication_ns,
            "durable_sidecar_receipt": receipt_ns,
            "total": total_ns,
        },
        "publication_mode": publication_mode,
        "delivery": _relative_record(delivery, screen_root),
        "sidecar_receipt": _relative_record(sidecar_path, screen_root),
    }


def _snapshot_evidence(
    source: Path, destination: Path, screen_root: Path, *, maximum: int
) -> dict[str, Any]:
    data, _ = _read_regular(source, maximum=maximum, label="evidence source")
    _publish_bytes_new(destination, data)
    return _relative_record(destination, screen_root)


def _png_identity(data: bytes) -> tuple[int, int, str]:
    if (
        len(data) < 33
        or data[:8] != b"\x89PNG\r\n\x1a\n"
        or data[12:16] != b"IHDR"
        or int.from_bytes(data[8:12], "big") != 13
    ):
        raise CacheScreenError("artifact is not a canonical PNG input")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    bit_depth = data[24]
    color_type = data[25]
    if bit_depth != 8 or color_type not in {2, 6}:
        raise CacheScreenError("PNG is not RGB/RGBA 8-bit")
    return width, height, "RGB" if color_type == 2 else "RGBA"


def _manifest_projection(
    value: dict[str, Any], *, expected_frame: int, expected_sha256: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    if (
        value.get("schema_version") != 2
        or value.get("kind") != MANIFEST_KIND
        or value.get("phase") != "baseline"
        or not isinstance(value.get("artifact"), dict)
        or value["artifact"].get("sha256") != expected_sha256
        or not isinstance(value.get("render"), dict)
        or value["render"].get("frame") != expected_frame
        or value["render"].get("width") != DIMENSIONS[0]
        or value["render"].get("height") != DIMENSIONS[1]
        or value["render"].get("samples") != REFERENCE_SAMPLES
        or not isinstance(value.get("scene"), dict)
    ):
        raise CacheScreenError("baseline manifest identity mismatch")
    scene = value["scene"]
    scene_projection = {
        "bundle_bytes": scene.get("bundle_bytes"),
        "bundle_files": scene.get("bundle_files"),
        "bundle_sha256": scene.get("bundle_sha256"),
        "main_blend_sha256": scene.get("sha256"),
    }
    if (
        type(scene_projection["bundle_bytes"]) is not int
        or scene_projection["bundle_bytes"] <= 0
        or type(scene_projection["bundle_files"]) is not int
        or scene_projection["bundle_files"] <= 0
        or not _is_sha256(scene_projection["bundle_sha256"])
        or not _is_sha256(scene_projection["main_blend_sha256"])
    ):
        raise CacheScreenError("baseline scene identity mismatch")
    render = value["render"]
    recipe_projection = {
        key: render.get(key)
        for key in (
            "device",
            "engine",
            "height",
            "integrator_policy",
            "pixel_filter",
            "png_compression",
            "sample_offset",
            "samples",
            "width",
            "worker_renderer_identity",
        )
    }
    return scene_projection, recipe_projection


def _quality_summary(value: dict[str, Any]) -> dict[str, Any]:
    try:
        black = value["mattes"]["black"]["metrics"]
        white = value["mattes"]["white"]["metrics"]
        return {
            "alpha_agreement": value["alpha_agreement"]["value"],
            "black": {
                "global_rgb_agreement": black["global_rgb_agreement"]["value"],
                "worst_regional_rgb_agreement": black[
                    "worst_regional_rgb_agreement"
                ]["value"],
                "worst_microtile_rgb_agreement": black[
                    "worst_microtile_rgb_agreement"
                ]["value"],
                "gaussian_luma_ssim": black["gaussian_luma_ssim"]["value"],
                "sobel_gms_mean": black["sobel_gms_mean"]["value"],
            },
            "white": {
                "global_rgb_agreement": white["global_rgb_agreement"]["value"],
                "worst_regional_rgb_agreement": white[
                    "worst_regional_rgb_agreement"
                ]["value"],
                "worst_microtile_rgb_agreement": white[
                    "worst_microtile_rgb_agreement"
                ]["value"],
                "gaussian_luma_ssim": white["gaussian_luma_ssim"]["value"],
                "sobel_gms_mean": white["sobel_gms_mean"]["value"],
            },
        }
    except (KeyError, TypeError) as exc:
        raise CacheScreenError("quality-v3 metrics are incomplete") from exc


def _validate_quality_pair(
    proof_path: Path,
    verification_path: Path,
    *,
    candidate: dict[str, Any],
    reference: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    proof, _ = _read_json(proof_path, "quality-v3 proof")
    verification, _ = _read_json(
        verification_path, "quality-v3 independent verification"
    )
    expected_candidate = {
        "bytes": candidate["bytes"],
        "sha256": candidate["sha256"],
    }
    expected_reference = {
        "bytes": reference["bytes"],
        "sha256": reference["sha256"],
    }
    try:
        proof_candidate = proof["inputs"]["candidate"]
        proof_reference = proof["inputs"]["reference"]
        verified_candidate = verification["artifacts"]["candidate"]
        verified_reference = verification["artifacts"]["reference"]
    except (KeyError, TypeError) as exc:
        raise CacheScreenError("quality evidence bindings are incomplete") from exc
    proof_result_sha = sha256_bytes(canonical_json(proof))
    if (
        proof.get("schema_version") != 3
        or proof.get("kind") != QUALITY_KIND
        or proof.get("pass") is not True
        or proof.get("failures") != []
        or proof.get("errors") != []
        or proof.get("inputs", {}).get("target_dimensions") != list(DIMENSIONS)
        or {key: proof_candidate.get(key) for key in ("bytes", "sha256")}
        != expected_candidate
        or {key: proof_reference.get(key) for key in ("bytes", "sha256")}
        != expected_reference
        or proof_candidate.get("mode") not in {"RGB", "RGBA"}
        or proof_reference.get("mode") not in {"RGB", "RGBA"}
        or verification.get("schema_version") != 1
        or verification.get("kind") != VERIFICATION_KIND
        or verification.get("pass") is not True
        or verification.get("proof_verified") is not True
        or verification.get("quality_pass") is not True
        or verification.get("errors") != []
        or verified_candidate != expected_candidate
        or verified_reference != expected_reference
        or verification.get("proof_result_sha256") != proof_result_sha
        or verification.get("recomputed_result_sha256") != proof_result_sha
    ):
        raise CacheScreenError("quality-v3 independent verification did not close")
    return proof, verification, _quality_summary(proof)


def _recompute_independent_verification(
    proof_path: Path, candidate_path: Path, reference_path: Path
) -> dict[str, Any]:
    try:
        import verify_cx_render_quality_v3 as verifier  # noqa: PLC0415

        result = verifier.verify_paths(proof_path, candidate_path, reference_path)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:
        raise CacheScreenError("independent quality verifier execution failed") from exc
    if not isinstance(result, dict):
        raise CacheScreenError("independent quality verifier returned invalid data")
    return result


def _external_artifact_record(path: Path, label: str) -> dict[str, Any]:
    data, info = _read_regular(path, maximum=MAX_ARTIFACT_BYTES, label=label)
    return {"bytes": info.st_size, "sha256": sha256_bytes(data)}


def _load_target_inputs(
    *,
    frame: int,
    receipt_path: Path,
    proof_path: Path,
    verification_path: Path,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    receipt, receipt_data = _read_json(receipt_path, f"frame-{frame} receipt")
    if (
        receipt.get("kind") != FRONTIER_KIND
        or receipt.get("schema_version") != 1
        or receipt.get("frame") != frame
        or receipt.get("reference", {}).get("samples") != REFERENCE_SAMPLES
    ):
        raise CacheScreenError(f"frame-{frame} frontier receipt mismatch")
    baseline_s = _finite_positive(receipt.get("baseline_s"), "target baseline")
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, dict):
        raise CacheScreenError("target receipt artifact table is missing")
    baseline_entry = artifacts.get("baseline")
    manifest_entry = artifacts.get("baseline_manifest")
    if not isinstance(baseline_entry, dict) or not isinstance(manifest_entry, dict):
        raise CacheScreenError("target baseline artifacts are missing")
    baseline_path = _resolve_beneath(
        receipt_path.parent, baseline_entry.get("path"), "target baseline"
    )
    baseline = _external_artifact_record(baseline_path, "target baseline PNG")
    if (
        baseline.get("bytes") != baseline_entry.get("bytes")
        or baseline.get("sha256") != baseline_entry.get("sha256")
    ):
        raise CacheScreenError("target baseline PNG does not match receipt")
    manifest_path = _resolve_beneath(
        receipt_path.parent, manifest_entry.get("path"), "target manifest"
    )
    manifest, manifest_data = _read_json(manifest_path, "target baseline manifest")
    if (
        len(manifest_data) != manifest_entry.get("bytes")
        or sha256_bytes(manifest_data) != manifest_entry.get("sha256")
    ):
        raise CacheScreenError("target baseline manifest does not match receipt")
    scene, recipe = _manifest_projection(
        manifest, expected_frame=frame, expected_sha256=baseline["sha256"]
    )
    proof, verification, summary = _validate_quality_pair(
        proof_path,
        verification_path,
        candidate=candidate,
        reference=baseline,
    )
    return {
        "frame": frame,
        "baseline_s": baseline_s,
        "baseline_path": baseline_path,
        "baseline": baseline,
        "receipt_path": receipt_path,
        "receipt_sha256": sha256_bytes(receipt_data),
        "manifest_path": manifest_path,
        "manifest_sha256": sha256_bytes(manifest_data),
        "scene": scene,
        "recipe": recipe,
        "proof_path": proof_path,
        "proof": proof,
        "verification_path": verification_path,
        "verification": verification,
        "quality_summary": summary,
    }


def assess_serialized_fingerprint(
    scenes: Sequence[dict[str, Any]], recipes: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    if not scenes or len(scenes) != len(recipes):
        raise CacheScreenError("fingerprint assessment inputs are incomplete")
    serialized_inputs_equal = all(scene == scenes[0] for scene in scenes[1:])
    render_recipes_equal = all(recipe == recipes[0] for recipe in recipes[1:])
    cheap_payload = {
        "serialized_scene_identity": scenes[0] if serialized_inputs_equal else scenes,
        "render_recipe_excluding_frame_and_seed": (
            recipes[0] if render_recipes_equal else recipes
        ),
    }
    return {
        "cheap_serialized_fingerprint_sha256": sha256_bytes(
            canonical_json(cheap_payload)
        ),
        "serialized_inputs_equal": serialized_inputs_equal,
        "render_recipes_equal_excluding_frame_and_seed": render_recipes_equal,
        "status": "insufficient_for_fail_closed_cross_frame_authorization",
        "cross_frame_reuse_authorized": False,
        "reference_free": False,
        "reason_codes": [
            "frame_changes_blender_dependency_graph_evaluation",
            "serialized_bytes_do_not_bind_evaluated_animation_drivers_constraints_or_simulation",
            "serialized_bytes_do_not_bind_all_geometry_nodes_compositor_handlers_or_python_side_effects",
            "complete_evaluated_render_state_has_no_frozen_canonical_cheap_serializer",
            "quality_v3_comparisons_are_posthoc_and_reference_dependent",
        ],
        "value_boundary": (
            "Reuse is valuable only for an externally authorized static-frame "
            "class. A general fail-closed claim would require a trusted complete "
            "evaluated-state invalidation oracle; evaluating and canonicalizing "
            "that state removes the cheap lookup proposition and remains incomplete."
        ),
    }


def _validate_record(record: Any, root: Path, label: str) -> Path:
    if not isinstance(record, dict) or set(record) != {"bytes", "path", "sha256"}:
        raise CacheScreenError(f"{label} artifact record shape mismatch")
    if (
        type(record["bytes"]) is not int
        or not 1 <= record["bytes"] <= MAX_ARTIFACT_BYTES
        or not _is_sha256(record["sha256"])
    ):
        raise CacheScreenError(f"{label} artifact identity mismatch")
    path = _resolve_beneath(root, record["path"], label)
    data, info = _read_regular(path, maximum=MAX_ARTIFACT_BYTES, label=label)
    if info.st_size != record["bytes"] or sha256_bytes(data) != record["sha256"]:
        raise CacheScreenError(f"{label} artifact digest mismatch")
    return path


def _validate_target_evidence_files(
    target: dict[str, Any], *, frame: int, baseline_s: float, screen_root: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    key = f"frame_{frame}"
    reference_path = _validate_record(
        target["reference"], screen_root, f"{key} fresh reference"
    )
    target_receipt_path = _validate_record(
        target["frontier_receipt"], screen_root, f"{key} frontier receipt"
    )
    target_manifest_path = _validate_record(
        target["baseline_manifest"], screen_root, f"{key} baseline manifest"
    )
    target_receipt, _ = _read_json(
        target_receipt_path, f"{key} frontier receipt"
    )
    target_manifest, _ = _read_json(
        target_manifest_path, f"{key} baseline manifest"
    )
    receipt_baseline = target_receipt.get("artifacts", {}).get("baseline", {})
    receipt_manifest = target_receipt.get("artifacts", {}).get(
        "baseline_manifest", {}
    )
    if (
        target_receipt.get("kind") != FRONTIER_KIND
        or target_receipt.get("schema_version") != 1
        or target_receipt.get("frame") != frame
        or target_receipt.get("reference", {}).get("samples")
        != REFERENCE_SAMPLES
        or not math.isclose(
            _finite_positive(
                target_receipt.get("baseline_s"), f"{key} receipt baseline"
            ),
            baseline_s,
            rel_tol=0.0,
            abs_tol=0.0,
        )
        or receipt_baseline.get("bytes") != target["reference"]["bytes"]
        or receipt_baseline.get("sha256") != target["reference"]["sha256"]
        or receipt_manifest.get("bytes") != target["baseline_manifest"]["bytes"]
        or receipt_manifest.get("sha256")
        != target["baseline_manifest"]["sha256"]
        or reference_path.is_symlink()
    ):
        raise CacheScreenError(f"{key} frontier receipt binding mismatch")
    target_scene, target_recipe = _manifest_projection(
        target_manifest,
        expected_frame=frame,
        expected_sha256=target["reference"]["sha256"],
    )
    if (
        target_scene != target["serialized_scene_identity"]
        or target_recipe != target["render_recipe_excluding_frame_and_seed"]
    ):
        raise CacheScreenError(f"{key} manifest projection mismatch")
    return target_scene, target_recipe


def _validate_quality_audit_files(
    *,
    label: str,
    audit: dict[str, Any],
    candidate_record: dict[str, Any],
    reference_record: dict[str, Any],
    screen_root: Path,
    pins: dict[str, str],
) -> None:
    candidate_path = _validate_record(
        audit["candidate"], screen_root, f"{label} candidate artifact"
    )
    reference_path = _validate_record(
        audit["reference"], screen_root, f"{label} reference artifact"
    )
    proof_path = _validate_record(
        audit["proof"], screen_root, f"{label} quality proof"
    )
    verification_path = _validate_record(
        audit["verification"], screen_root, f"{label} quality verification"
    )
    proof, verification, recomputed_summary = _validate_quality_pair(
        proof_path,
        verification_path,
        candidate=candidate_record,
        reference=reference_record,
    )
    independently_recomputed = _recompute_independent_verification(
        proof_path, candidate_path, reference_path
    )
    if (
        proof.get("contract_sha256") != pins["quality_v3_contract_sha256"]
        or proof.get("runtime", {}).get("metric_module_sha256")
        != pins["quality_v3_module_sha256"]
        or verification.get("verifier")
        != "cx-render-preview-quality-v3-verifier-v1"
        or recomputed_summary != audit["summary"]
        or canonical_json(independently_recomputed) != canonical_json(verification)
    ):
        raise CacheScreenError(f"{label} quality evidence changed")


def validate_screen_receipt(
    receipt: Any, screen_root: Path, *, verify_files: bool = True
) -> None:
    if not isinstance(receipt, dict) or set(receipt) != {
        "artifacts",
        "authorization",
        "claim_scope",
        "evidence",
        "fingerprint_assessment",
        "host",
        "kind",
        "measurement",
        "pins",
        "quality_audits",
        "schema_version",
        "scope_partitioned",
        "source",
        "timing_scope",
    }:
        raise CacheScreenError("screen receipt shape mismatch")
    if (
        receipt["schema_version"] != SCHEMA_VERSION
        or receipt["kind"] != RECEIPT_KIND
        or receipt["evidence"] not in {"measured", "synthetic"}
        or receipt["scope_partitioned"] is not True
        or receipt["authorization"]
        != {
            "cross_frame_audit": {
                "cache_selection": "posthoc_quality_v3_known_frames",
                "cross_frame_generalization_authorized": False,
                "production_authorizable": False,
                "product_decision_reference_free": False,
                "reference_used_for_audit": True,
            },
            "exact_transport": {
                "artifact_eligibility_inherited": True,
                "byte_identity_required": True,
                "cross_frame": False,
                "transport_authorized": True,
                "query_identity": (
                    "exact_frame_scene_recipe_policy_runtime_and_output_contract"
                ),
                "storage_trust_assumption": (
                    "same_uid_storage_writers_are_trusted_not_to_mutate_during_"
                    "the_transaction_or_after_return_and_each_consumption_is_"
                    "fully_sha256_validated"
                ),
                "transport": "descriptor_copy_hash_fsync_no_clobber",
            },
        }
        or receipt["fingerprint_assessment"].get(
            "cross_frame_reuse_authorized"
        )
        is not False
        or receipt["fingerprint_assessment"].get("reference_free") is not False
    ):
        raise CacheScreenError("screen authorization boundary mismatch")
    pins = receipt["pins"]
    if (
        not isinstance(pins, dict)
        or set(pins)
        != {
            "quality_v3_contract_sha256",
            "quality_v3_module_sha256",
            "quality_v3_verifier_sha256",
            "screen_module_sha256",
            "screen_test_sha256",
        }
        or pins != implementation_pins()
    ):
        raise CacheScreenError("screen implementation pins mismatch")
    source = receipt["source"]
    if (
        not isinstance(source, dict)
        or source.get("frame") != 9
        or source.get("samples") != REFERENCE_SAMPLES
        or source.get("dimensions") != list(DIMENSIONS)
        or not isinstance(source.get("artifact"), dict)
        or not isinstance(source.get("request_identity"), dict)
        or not isinstance(source.get("eligibility"), dict)
        or not isinstance(source.get("serialized_scene_identity"), dict)
    ):
        raise CacheScreenError("screen source identity mismatch")
    if source["serialized_scene_identity"] != _request_scene_projection(
        source["request_identity"]
    ):
        raise CacheScreenError("source/request serialized scene binding mismatch")
    source_eligibility = source["eligibility"]
    if (
        set(source_eligibility)
        != {
            "artifact_verified",
            "billing_eligible",
            "preview_only",
            "production_ready",
        }
        or any(type(value) is not bool for value in source_eligibility.values())
        or source_eligibility
        != {
            key: source["request_identity"]["expected_output_contract"][key]
            for key in source_eligibility
        }
    ):
        raise CacheScreenError("source artifact eligibility binding mismatch")
    concrete_production_eligible = (
        source_eligibility["production_ready"]
        and source_eligibility["artifact_verified"]
        and not source_eligibility["preview_only"]
    )
    measurement = receipt["measurement"]
    if not isinstance(measurement, dict) or set(measurement) != {
        "excluded_from_headline",
        "headline_scopes",
        "included_stages",
        "maximum_s",
        "median_s",
        "minimum_s",
        "p95_s_type7",
        "population_variance_s2",
        "samples_s",
        "trial_count",
        "trials",
    }:
        raise CacheScreenError("screen measurement shape mismatch")
    trials = measurement["trials"]
    samples = measurement["samples_s"]
    count = measurement["trial_count"]
    if (
        type(count) is not int
        or not 3 <= count <= 31
        or count % 2 == 0
        or not isinstance(trials, list)
        or not isinstance(samples, list)
        or len(trials) != count
        or len(samples) != count
        or measurement["included_stages"]
        != [
            "strict_cache_index_lookup",
            "full_cached_artifact_sha256_validation",
            "durable_no_clobber_delivery_publication",
            "durable_bound_sidecar_receipt_publication",
        ]
    ):
        raise CacheScreenError("screen trial identity mismatch")
    values = [_finite_positive(value, "timing sample") for value in samples]
    expected_stats = {
        "median_s": statistics.median(values),
        "p95_s_type7": _type7_quantile(values, 0.95),
        "minimum_s": min(values),
        "maximum_s": max(values),
        "population_variance_s2": statistics.pvariance(values),
    }
    for key, expected in expected_stats.items():
        observed = measurement.get(key)
        if (
            isinstance(observed, bool)
            or not isinstance(observed, (int, float))
            or not math.isfinite(float(observed))
            or not math.isclose(
                float(observed), expected, rel_tol=1e-12, abs_tol=1e-12
            )
        ):
            raise CacheScreenError(f"screen {key} mismatch")
    median = expected_stats["median_s"]
    scopes = measurement["headline_scopes"]
    if not isinstance(scopes, dict) or set(scopes) != {
        "cross_frame_audit",
        "exact_transport",
    }:
        raise CacheScreenError("screen headline scope partition mismatch")
    exact = scopes["exact_transport"]
    cross = scopes["cross_frame_audit"]
    if (
        not isinstance(exact, dict)
        or exact.get("scope")
        != "exact_request_byte_transport_with_inherited_source_eligibility"
        or exact.get("transport_authorized") is not True
        or exact.get("artifact_eligibility_inherited") is not True
        or exact.get("source_eligibility") != source_eligibility
        or exact.get("concrete_artifact_production_eligible")
        is not concrete_production_eligible
        or exact.get("concrete_artifact_billing_eligible")
        is not source_eligibility["billing_eligible"]
        or exact.get("cross_frame") is not False
        or exact.get("transport")
        != "descriptor_copy_hash_fsync_no_clobber"
        or exact.get("storage_trust_assumption")
        != (
            "same_uid_storage_writers_are_trusted_not_to_mutate_during_the_"
            "transaction_or_after_return_and_each_consumption_is_fully_sha256_"
            "validated"
        )
        or exact.get("request_cache_key") != source.get("cache_key")
        or exact.get("quality_identity", {}).get("equivalence")
        != "sha256_exact_bytes"
        or exact.get("quality_identity", {}).get(
            "all_trial_deliveries_byte_identical"
        )
        is not True
        or not isinstance(cross, dict)
        or cross.get("scope")
        != "posthoc_approximate_cross_frame_quality_v3_audit"
        or cross.get("audit_only") is not True
        or cross.get("production_authorizable") is not False
        or cross.get("independent_quality_v3_verified") is not True
    ):
        raise CacheScreenError("screen headline scope identity mismatch")
    exact_baseline = _finite_positive(exact.get("baseline_s"), "exact baseline")
    exact_threshold = exact_baseline / 1000.0
    if (
        not math.isclose(
            exact.get("median_speedup_x", 0.0),
            exact_baseline / median,
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
        or not math.isclose(
            exact.get("p95_s_type7", -1.0),
            expected_stats["p95_s_type7"],
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
        or not math.isclose(
            exact.get("slowest_s", -1.0),
            max(values),
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
        or not math.isclose(
            exact.get("slowest_speedup_x", 0.0),
            exact_baseline / max(values),
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
        or not math.isclose(
            exact.get("per_trial_1000x_latency_ceiling_s", -1.0),
            exact_threshold,
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
        or exact.get("all_9_trials_exceed_1000x")
        is not (count == 9 and all(value < exact_threshold for value in values))
    ):
        raise CacheScreenError("exact-transport headline calculation mismatch")
    baselines = cross.get("baselines_s")
    speedups = cross.get("median_speedup_x")
    if set(baselines) != {"frame_10", "frame_11"} or set(speedups) != {
        "frame_10",
        "frame_11",
    }:
        raise CacheScreenError("screen baseline set mismatch")
    expected_pass = True
    for key in ("frame_10", "frame_11"):
        baseline = _finite_positive(baselines[key], f"{key} baseline")
        speedup = _finite_positive(speedups[key], f"{key} speedup")
        if not math.isclose(speedup, baseline / median, rel_tol=1e-12, abs_tol=1e-12):
            raise CacheScreenError(f"{key} speedup mismatch")
        expected_pass = expected_pass and speedup >= 1000.0
    if cross.get("median_exceeds_1000x_on_all_targets") is not expected_pass:
        raise CacheScreenError("screen 1000x decision mismatch")

    artifacts = receipt["artifacts"]
    if not isinstance(artifacts, dict) or set(artifacts) != {
        "cache_index",
        "cache_source",
        "source_performance_receipt",
        "source_manifest",
        "targets",
    }:
        raise CacheScreenError("screen artifact table mismatch")
    target_table = artifacts["targets"]
    if not isinstance(target_table, dict) or set(target_table) != {
        "frame_10",
        "frame_11",
    }:
        raise CacheScreenError("target artifact table mismatch")
    for frame in (10, 11):
        key = f"frame_{frame}"
        target = target_table[key]
        if not isinstance(target, dict) or set(target) != {
            "baseline_manifest",
            "baseline_s",
            "frame",
            "frontier_receipt",
            "reference",
            "render_recipe_excluding_frame_and_seed",
            "serialized_scene_identity",
        }:
            raise CacheScreenError(f"{key} target record shape mismatch")
        if (
            target["frame"] != frame
            or not math.isclose(
                _finite_positive(target["baseline_s"], f"{key} target baseline"),
                baselines[key],
                rel_tol=0.0,
                abs_tol=0.0,
            )
            or not isinstance(target["serialized_scene_identity"], dict)
            or not isinstance(
                target["render_recipe_excluding_frame_and_seed"], dict
            )
        ):
            raise CacheScreenError(f"{key} target identity mismatch")
    if verify_files:
        index_path = _validate_record(
            artifacts["cache_index"], screen_root, "cache index"
        )
        source_path = _validate_record(
            artifacts["cache_source"], screen_root, "cache source"
        )
        source_manifest_path = _validate_record(
            artifacts["source_manifest"], screen_root, "source manifest"
        )
        performance_path = _validate_record(
            artifacts["source_performance_receipt"],
            screen_root,
            "source performance receipt",
        )
        performance, _ = _read_json(
            performance_path, "source performance receipt"
        )
        if (
            performance.get("kind") != "cx_local_cycles_spec_benchmark"
            or performance.get("frame") != 9
            or performance.get("reference_samples") != REFERENCE_SAMPLES
            or not math.isclose(
                _finite_positive(
                    performance.get("baseline_s"), "source performance baseline"
                ),
                exact_baseline,
                rel_tol=0.0,
                abs_tol=0.0,
            )
        ):
            raise CacheScreenError("source performance baseline binding mismatch")
        index, indexed_source_path, _ = _lookup_cache_index(
            index_path,
            expected_sha256=artifacts["cache_index"]["sha256"],
            expected_key=receipt["source"]["cache_key"],
        )
        if indexed_source_path != source_path:
            raise CacheScreenError("cache index/source resolved path binding mismatch")
        if source["request_identity"] != index["request_identity"]:
            raise CacheScreenError("exact request identity table binding mismatch")
        source_fd, source_info, _ = _open_and_hash_source(
            indexed_source_path,
            expected_sha256=source["artifact"]["sha256"],
            expected_bytes=source["artifact"]["bytes"],
        )
        try:
            if source_info.st_size != artifacts["cache_source"]["bytes"]:
                raise CacheScreenError("retained cache source byte binding mismatch")
            source_manifest, _ = _read_json(
                source_manifest_path, "source baseline manifest"
            )
            recomputed_request_identity = request_identity_from_manifest(
                source_manifest
            )
            if (
                recomputed_request_identity != index["request_identity"]
                or recomputed_request_identity != source["request_identity"]
            ):
                raise CacheScreenError(
                    "source manifest exact request identity recomputation mismatch"
                )
            if source_eligibility_from_manifest(source_manifest) != source_eligibility:
                raise CacheScreenError(
                    "source manifest eligibility inheritance mismatch"
                )
            source_scene, source_recipe = _manifest_projection(
                source_manifest,
                expected_frame=9,
                expected_sha256=source["artifact"]["sha256"],
            )
            _validate_source_performance_binding(
                performance=performance,
                source=source,
                source_manifest=source_manifest,
                exact_baseline=exact_baseline,
            )
            _validate_aggregate_cache_source_tuple(
                index=index,
                source=source,
                cache_source_record=artifacts["cache_source"],
                source_manifest_record=artifacts["source_manifest"],
                cache_source_basename=source_path.name,
                source_manifest_basename=source_manifest_path.name,
                manifest_scene=source_scene,
            )
        finally:
            os.close(source_fd)
        for frame in (10, 11):
            key = f"frame_{frame}"
            target = target_table[key]
            _validate_target_evidence_files(
                target,
                frame=frame,
                baseline_s=target["baseline_s"],
                screen_root=screen_root,
            )
        expected_fingerprint = assess_serialized_fingerprint(
            [
                source_scene,
                target_table["frame_10"]["serialized_scene_identity"],
                target_table["frame_11"]["serialized_scene_identity"],
            ],
            [
                source_recipe,
                target_table["frame_10"][
                    "render_recipe_excluding_frame_and_seed"
                ],
                target_table["frame_11"][
                    "render_recipe_excluding_frame_and_seed"
                ],
            ],
        )
        if receipt["fingerprint_assessment"] != expected_fingerprint:
            raise CacheScreenError("serialized fingerprint assessment mismatch")

    for index, (trial, sample) in enumerate(zip(trials, values, strict=True)):
        if (
            not isinstance(trial, dict)
            or trial.get("trial_index") != index
            or trial.get("publication_mode") != "copy"
            or not math.isclose(
                _finite_positive(trial.get("total_s"), "trial total"),
                sample,
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
            or not isinstance(trial.get("timings_ns"), dict)
        ):
            raise CacheScreenError("screen trial record mismatch")
        timings = trial["timings_ns"]
        if set(timings) != {
            "cache_lookup",
            "durable_artifact_publication",
            "durable_sidecar_receipt",
            "full_sha256_validation",
            "total",
        } or any(type(value) is not int or value <= 0 for value in timings.values()):
            raise CacheScreenError("screen trial timing closure mismatch")
        included_total = sum(
            timings[key]
            for key in (
                "cache_lookup",
                "full_sha256_validation",
                "durable_artifact_publication",
                "durable_sidecar_receipt",
            )
        )
        if timings["total"] < included_total:
            raise CacheScreenError("screen trial total omits an included stage")
        if not math.isclose(
            timings["total"] / 1_000_000_000,
            sample,
            rel_tol=0.0,
            abs_tol=0.5e-9,
        ):
            raise CacheScreenError("screen trial seconds/nanoseconds mismatch")
        if verify_files:
            delivery_path = _validate_record(
                trial["delivery"], screen_root, "trial delivery"
            )
            sidecar_path = _validate_record(
                trial["sidecar_receipt"], screen_root, "trial sidecar"
            )
            if (
                trial["delivery"]["bytes"] != source["artifact"]["bytes"]
                or trial["delivery"]["sha256"] != source["artifact"]["sha256"]
            ):
                raise CacheScreenError("trial delivery source binding mismatch")
            sidecar, _ = _read_json(sidecar_path, "trial sidecar")
            expected_completed = {
                "cache_lookup": timings["cache_lookup"],
                "full_sha256_validation": timings["full_sha256_validation"],
                "durable_artifact_publication": timings[
                    "durable_artifact_publication"
                ],
            }
            if (
                set(sidecar)
                != {
                    "authorization",
                    "cache_index_sha256",
                    "cache_key",
                    "completed_stage_ns",
                    "kind",
                    "output",
                    "publication_mode",
                    "schema_version",
                    "source",
                    "timing_scope",
                    "trial_index",
                }
                or
                sidecar.get("kind") != DELIVERY_KIND
                or sidecar.get("schema_version") != SCHEMA_VERSION
                or sidecar.get("trial_index") != index
                or sidecar.get("authorization")
                != {
                    "exact_transport_authorized": True,
                    "cross_frame_reuse": False,
                    "source_artifact_eligibility_inherited": True,
                }
                or sidecar.get("cache_index_sha256")
                != artifacts["cache_index"]["sha256"]
                or sidecar.get("cache_key") != source["cache_key"]
                or sidecar.get("source")
                != {
                    "bytes": source["artifact"]["bytes"],
                    "sha256": source["artifact"]["sha256"],
                    "source_frame": 9,
                    "samples": REFERENCE_SAMPLES,
                }
                or sidecar.get("output", {}).get("path")
                != trial["delivery"]["path"]
                or sidecar.get("output", {}).get("bytes")
                != trial["delivery"]["bytes"]
                or sidecar.get("output", {}).get("sha256")
                != trial["delivery"]["sha256"]
                or sidecar.get("publication_mode") != trial["publication_mode"]
                or sidecar.get("completed_stage_ns") != expected_completed
                or delivery_path.is_symlink()
            ):
                raise CacheScreenError("trial sidecar binding mismatch")

    audits = receipt["quality_audits"]
    if not isinstance(audits, dict) or set(audits) != {
        "frame_10_vs_frame_11",
        "frame_9_vs_frame_10",
        "frame_9_vs_frame_11",
    }:
        raise CacheScreenError("quality audit set mismatch")
    expected_audits = {
        "frame_9_vs_frame_10": (
            9,
            10,
            source["artifact"],
            target_table["frame_10"]["reference"],
        ),
        "frame_9_vs_frame_11": (
            9,
            11,
            source["artifact"],
            target_table["frame_11"]["reference"],
        ),
        "frame_10_vs_frame_11": (
            10,
            11,
            target_table["frame_10"]["reference"],
            target_table["frame_11"]["reference"],
        ),
    }
    observed_pairs: set[tuple[int, int]] = set()
    for label, audit in audits.items():
        candidate_frame, reference_frame, candidate_record, reference_record = (
            expected_audits[label]
        )
        if (
            not isinstance(audit, dict)
            or set(audit)
            != {
                "candidate",
                "candidate_frame",
                "pass",
                "proof",
                "proof_verified",
                "reference",
                "reference_frame",
                "summary",
                "verification",
            }
            or audit.get("candidate_frame") != candidate_frame
            or audit.get("reference_frame") != reference_frame
            or audit.get("candidate") != candidate_record
            or audit.get("reference") != reference_record
            or audit.get("pass") is not True
            or audit.get("proof_verified") is not True
            or not isinstance(audit.get("summary"), dict)
        ):
            raise CacheScreenError(f"{label} quality audit mismatch")
        pair = (candidate_frame, reference_frame)
        if pair in observed_pairs:
            raise CacheScreenError("quality audit frame pair is duplicated")
        observed_pairs.add(pair)
        if verify_files:
            _validate_quality_audit_files(
                label=label,
                audit=audit,
                candidate_record=candidate_record,
                reference_record=reference_record,
                screen_root=screen_root,
                pins=pins,
            )


def _prepare_screen_root(path: Path) -> tuple[Path, Path, Path]:
    if path.exists() or path.is_symlink():
        raise CacheScreenError("screen output root already exists")
    _require_real_directory(path.parent, "screen output parent")
    os.mkdir(path, 0o700)
    cache = path / "cache"
    evidence = path / "evidence"
    trials = path / "trials"
    os.mkdir(cache, 0o700)
    os.mkdir(evidence, 0o700)
    os.mkdir(trials, 0o700)
    _fsync_directory(path)
    _fsync_directory(path.parent)
    return cache, evidence, trials


def run_screen(args: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    if not args.force_copy:
        raise CacheScreenError(
            "exact-transport receipt requires descriptor copy; "
            "hard links are trusted-immutable-cache experiments only"
        )
    source_path = Path(args.source).resolve(strict=True)
    source_manifest_path = Path(args.source_manifest).resolve(strict=True)
    source_performance_path = Path(args.source_performance_receipt).resolve(
        strict=True
    )
    source_data, source_info = _read_regular(
        source_path, maximum=MAX_ARTIFACT_BYTES, label="frame-9 cache source"
    )
    width, height, mode = _png_identity(source_data)
    source_identity = {
        "bytes": source_info.st_size,
        "sha256": sha256_bytes(source_data),
    }
    if (width, height) != DIMENSIONS or mode != "RGBA":
        raise CacheScreenError("frame-9 cache source PNG identity mismatch")
    source_manifest, source_manifest_data = _read_json(
        source_manifest_path, "frame-9 baseline manifest"
    )
    source_scene, source_recipe = _manifest_projection(
        source_manifest,
        expected_frame=9,
        expected_sha256=source_identity["sha256"],
    )
    exact_request_identity = request_identity_from_manifest(source_manifest)
    source_eligibility = source_eligibility_from_manifest(source_manifest)
    source_performance, source_performance_data = _read_json(
        source_performance_path, "frame-9 performance receipt"
    )
    if (
        source_performance.get("kind") != "cx_local_cycles_spec_benchmark"
        or source_performance.get("schema_version") != 1
        or source_performance.get("frame") != 9
        or source_performance.get("reference_samples") != REFERENCE_SAMPLES
    ):
        raise CacheScreenError("frame-9 performance receipt identity mismatch")
    exact_baseline_s = _finite_positive(
        source_performance.get("baseline_s"), "frame-9 exact-transport baseline"
    )
    target_specs = args.target or []
    if len(target_specs) != 2 or {int(row[0]) for row in target_specs} != {10, 11}:
        raise CacheScreenError("exactly frame-10 and frame-11 targets are required")
    targets: list[dict[str, Any]] = []
    for raw_frame, receipt, proof, verification in target_specs:
        targets.append(
            _load_target_inputs(
                frame=int(raw_frame),
                receipt_path=Path(receipt).resolve(strict=True),
                proof_path=Path(proof).resolve(strict=True),
                verification_path=Path(verification).resolve(strict=True),
                candidate=source_identity,
            )
        )
    targets.sort(key=lambda row: row["frame"])
    target_by_frame = {row["frame"]: row for row in targets}
    cross_specs = args.cross_check or []
    if len(cross_specs) != 1 or [int(value) for value in cross_specs[0][:2]] != [10, 11]:
        raise CacheScreenError("the frame-10 versus frame-11 cross-check is required")
    _, _, cross_proof_path, cross_verification_path = cross_specs[0]
    cross_proof, cross_verification, cross_summary = _validate_quality_pair(
        Path(cross_proof_path).resolve(strict=True),
        Path(cross_verification_path).resolve(strict=True),
        candidate=target_by_frame[10]["baseline"],
        reference=target_by_frame[11]["baseline"],
    )
    fingerprint = assess_serialized_fingerprint(
        [source_scene, *(target["scene"] for target in targets)],
        [source_recipe, *(target["recipe"] for target in targets)],
    )

    screen_root = Path(args.output_root).expanduser().absolute()
    cache_root, evidence_root, trials_root = _prepare_screen_root(screen_root)
    cache_source = cache_root / "source.png"
    cache_manifest = cache_root / "source-manifest.json"
    source_performance_snapshot = evidence_root / "frame-9-performance-receipt.json"
    _publish_bytes_new(cache_source, source_data)
    _publish_bytes_new(cache_manifest, source_manifest_data)
    _publish_bytes_new(source_performance_snapshot, source_performance_data)
    cache_source_record = _relative_record(cache_source, screen_root)
    source_manifest_record = _relative_record(cache_manifest, screen_root)
    source_performance_record = _relative_record(
        source_performance_snapshot, screen_root
    )
    index_artifact = {
        "path": cache_source.name,
        "bytes": cache_source_record["bytes"],
        "sha256": cache_source_record["sha256"],
        "dimensions": list(DIMENSIONS),
        "mode": mode,
        "samples": REFERENCE_SAMPLES,
        "source_frame": 9,
    }
    index_manifest = {
        "path": cache_manifest.name,
        "bytes": source_manifest_record["bytes"],
        "sha256": source_manifest_record["sha256"],
    }
    cache_index = build_cache_index(
        artifact=index_artifact,
        source_manifest=index_manifest,
        serialized_scene_identity=source_scene,
        request_identity=exact_request_identity,
    )
    cache_index_path = cache_root / "index.json"
    _publish_bytes_new(cache_index_path, canonical_json(cache_index) + b"\n")
    cache_index_record = _relative_record(cache_index_path, screen_root)

    quality_audits: dict[str, Any] = {}
    target_artifacts: dict[str, Any] = {}
    for target in targets:
        frame = target["frame"]
        reference_record = _snapshot_evidence(
            target["baseline_path"],
            evidence_root / f"frame-{frame}-reference.png",
            screen_root,
            maximum=MAX_ARTIFACT_BYTES,
        )
        frontier_receipt_record = _snapshot_evidence(
            target["receipt_path"],
            evidence_root / f"frame-{frame}-frontier-receipt.json",
            screen_root,
            maximum=MAX_JSON_BYTES,
        )
        baseline_manifest_record = _snapshot_evidence(
            target["manifest_path"],
            evidence_root / f"frame-{frame}-baseline-manifest.json",
            screen_root,
            maximum=MAX_JSON_BYTES,
        )
        proof_record = _snapshot_evidence(
            target["proof_path"],
            evidence_root / f"frame-9-vs-{frame}-quality-v3.json",
            screen_root,
            maximum=MAX_JSON_BYTES,
        )
        verification_record = _snapshot_evidence(
            target["verification_path"],
            evidence_root / f"frame-9-vs-{frame}-verification.json",
            screen_root,
            maximum=MAX_JSON_BYTES,
        )
        if reference_record["sha256"] != target["baseline"]["sha256"]:
            raise CacheScreenError("snapshotted target reference changed")
        target_artifacts[f"frame_{frame}"] = {
            "frame": frame,
            "baseline_s": target["baseline_s"],
            "reference": reference_record,
            "frontier_receipt": frontier_receipt_record,
            "baseline_manifest": baseline_manifest_record,
            "serialized_scene_identity": target["scene"],
            "render_recipe_excluding_frame_and_seed": target["recipe"],
        }
        quality_audits[f"frame_9_vs_frame_{frame}"] = {
            "candidate_frame": 9,
            "reference_frame": frame,
            "pass": True,
            "proof_verified": True,
            "candidate": cache_source_record,
            "reference": reference_record,
            "proof": proof_record,
            "verification": verification_record,
            "summary": target["quality_summary"],
        }
    cross_proof_source = Path(cross_proof_path).resolve(strict=True)
    cross_verification_source = Path(cross_verification_path).resolve(strict=True)
    quality_audits["frame_10_vs_frame_11"] = {
        "candidate_frame": 10,
        "reference_frame": 11,
        "pass": True,
        "proof_verified": True,
        "candidate": target_artifacts["frame_10"]["reference"],
        "reference": target_artifacts["frame_11"]["reference"],
        "proof": _snapshot_evidence(
            cross_proof_source,
            evidence_root / "frame-10-vs-11-quality-v3.json",
            screen_root,
            maximum=MAX_JSON_BYTES,
        ),
        "verification": _snapshot_evidence(
            cross_verification_source,
            evidence_root / "frame-10-vs-11-verification.json",
            screen_root,
            maximum=MAX_JSON_BYTES,
        ),
        "summary": cross_summary,
    }

    if type(args.trials) is not int or not 3 <= args.trials <= 31 or args.trials % 2 == 0:
        raise CacheScreenError("trial count must be odd and between 3 and 31")
    trial_records = [
        run_timed_trial(
            index_path=cache_index_path,
            index_sha256=cache_index_record["sha256"],
            cache_key=cache_index["cache_key"],
            trials_root=trials_root,
            screen_root=screen_root,
            trial_index=index,
            prefer_hardlink=not args.force_copy,
        )
        for index in range(args.trials)
    ]
    samples = [row["total_s"] for row in trial_records]
    median = statistics.median(samples)
    baselines = {
        f"frame_{target['frame']}": target["baseline_s"] for target in targets
    }
    speedups = {key: value / median for key, value in baselines.items()}
    p95 = _type7_quantile(samples, 0.95)
    slowest = max(samples)
    exact_ceiling = exact_baseline_s / 1000.0
    exact_quality_identity = {
        "equivalence": "sha256_exact_bytes",
        "source_sha256": cache_source_record["sha256"],
        "source_bytes": cache_source_record["bytes"],
        "all_trial_deliveries_byte_identical": all(
            trial["delivery"]["sha256"] == cache_source_record["sha256"]
            and trial["delivery"]["bytes"] == cache_source_record["bytes"]
            for trial in trial_records
        ),
    }
    measurement = {
        "trial_count": args.trials,
        "samples_s": samples,
        "median_s": median,
        "p95_s_type7": p95,
        "minimum_s": min(samples),
        "maximum_s": max(samples),
        "population_variance_s2": statistics.pvariance(samples),
        "included_stages": [
            "strict_cache_index_lookup",
            "full_cached_artifact_sha256_validation",
            "durable_no_clobber_delivery_publication",
            "durable_bound_sidecar_receipt_publication",
        ],
        "excluded_from_headline": [
            "cache_population",
            "posthoc_quality_v3_evaluation",
            "independent_quality_v3_verification",
            "fresh_4096_spp_target_references",
            "final_screen_receipt_publication_and_audit",
        ],
        "headline_scopes": {
            "exact_transport": {
                "scope": "exact_request_byte_transport_with_inherited_source_eligibility",
                "transport_authorized": True,
                "artifact_eligibility_inherited": True,
                "source_eligibility": source_eligibility,
                "concrete_artifact_production_eligible": (
                    source_eligibility["production_ready"]
                    and source_eligibility["artifact_verified"]
                    and not source_eligibility["preview_only"]
                ),
                "concrete_artifact_billing_eligible": source_eligibility[
                    "billing_eligible"
                ],
                "cross_frame": False,
                "transport": "descriptor_copy_hash_fsync_no_clobber",
                "storage_trust_assumption": (
                    "same_uid_storage_writers_are_trusted_not_to_mutate_during_"
                    "the_transaction_or_after_return_and_each_consumption_is_"
                    "fully_sha256_validated"
                ),
                "request_cache_key": cache_index["cache_key"],
                "baseline_s": exact_baseline_s,
                "median_speedup_x": exact_baseline_s / median,
                "p95_s_type7": p95,
                "slowest_s": slowest,
                "slowest_speedup_x": exact_baseline_s / slowest,
                "per_trial_1000x_latency_ceiling_s": exact_ceiling,
                "all_9_trials_exceed_1000x": (
                    args.trials == 9 and all(value < exact_ceiling for value in samples)
                ),
                "quality_identity": exact_quality_identity,
            },
            "cross_frame_audit": {
                "scope": "posthoc_approximate_cross_frame_quality_v3_audit",
                "audit_only": True,
                "production_authorizable": False,
                "independent_quality_v3_verified": True,
                "target_frames": [10, 11],
                "baselines_s": baselines,
                "median_speedup_x": speedups,
                "median_exceeds_1000x_on_all_targets": all(
                    value >= 1000.0 for value in speedups.values()
                ),
            },
        },
        "trials": trial_records,
    }
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "kind": RECEIPT_KIND,
        "evidence": "measured",
        "scope_partitioned": True,
        "claim_scope": (
            "partitioned exact-transport and cross-frame scopes: exact frame-9 "
            "request transport is authorized without upgrading its inherited "
            "preview/non-production eligibility; frames 10-11 are post-hoc audit only"
        ),
        "timing_scope": (
            "steady filesystem cache lookup through durable delivery and durable "
            "bound sidecar receipt; final aggregate receipt is excluded"
        ),
        "authorization": {
            "exact_transport": {
                "artifact_eligibility_inherited": True,
                "byte_identity_required": True,
                "cross_frame": False,
                "transport_authorized": True,
                "query_identity": (
                    "exact_frame_scene_recipe_policy_runtime_and_output_contract"
                ),
                "storage_trust_assumption": (
                    "same_uid_storage_writers_are_trusted_not_to_mutate_during_"
                    "the_transaction_or_after_return_and_each_consumption_is_"
                    "fully_sha256_validated"
                ),
                "transport": "descriptor_copy_hash_fsync_no_clobber",
            },
            "cross_frame_audit": {
                "cache_selection": "posthoc_quality_v3_known_frames",
                "cross_frame_generalization_authorized": False,
                "production_authorizable": False,
                "product_decision_reference_free": False,
                "reference_used_for_audit": True,
            },
        },
        "source": {
            "frame": 9,
            "samples": REFERENCE_SAMPLES,
            "dimensions": list(DIMENSIONS),
            "cache_key": cache_index["cache_key"],
            "request_identity": exact_request_identity,
            "eligibility": source_eligibility,
            "artifact": cache_source_record,
            "serialized_scene_identity": source_scene,
        },
        "fingerprint_assessment": fingerprint,
        "pins": implementation_pins(),
        "quality_audits": quality_audits,
        "measurement": measurement,
        "artifacts": {
            "cache_index": cache_index_record,
            "cache_source": cache_source_record,
            "source_performance_receipt": source_performance_record,
            "source_manifest": source_manifest_record,
            "targets": target_artifacts,
        },
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "pid": os.getpid(),
        },
    }
    validate_screen_receipt(receipt, screen_root, verify_files=True)
    receipt_path = screen_root / "receipt.json"
    _publish_bytes_new(receipt_path, canonical_json(receipt) + b"\n")
    published, _ = _read_json(receipt_path, "published screen receipt")
    validate_screen_receipt(published, screen_root, verify_files=True)
    return receipt, receipt_path


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="cached frame-9 4096-SPP PNG")
    parser.add_argument(
        "--source-manifest",
        required=True,
        help=(
            "frame-9 baseline manifest; exact transport inherits its preview, "
            "production, verification, and billing eligibility"
        ),
    )
    parser.add_argument(
        "--source-performance-receipt",
        required=True,
        help="frame-9 measured performance receipt containing the exact baseline",
    )
    parser.add_argument(
        "--target",
        nargs=4,
        action="append",
        metavar=("FRAME", "RECEIPT", "QUALITY_PROOF", "VERIFICATION"),
        help="fresh target frame receipt and frame-9 quality evidence",
    )
    parser.add_argument(
        "--cross-check",
        nargs=4,
        action="append",
        metavar=("CANDIDATE_FRAME", "REFERENCE_FRAME", "QUALITY_PROOF", "VERIFICATION"),
        help="required frame-10 versus frame-11 quality evidence",
    )
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    parser.add_argument(
        "--force-copy",
        action="store_true",
        help=(
            "required release transport: retained-FD copy, staged fsync, "
            "post-link inode/full-SHA validation, and no-clobber publication"
        ),
    )
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        receipt, receipt_path = run_screen(args)
        summary = {
            "pass": True,
            "scope_partitioned": True,
            "receipt_path": str(receipt_path),
            "median_s": receipt["measurement"]["median_s"],
            "p95_s_type7": receipt["measurement"]["p95_s_type7"],
            "slowest_s": receipt["measurement"]["maximum_s"],
            "exact_transport": receipt["measurement"]["headline_scopes"]
            ["exact_transport"],
            "cross_frame_audit": receipt["measurement"]["headline_scopes"]
            ["cross_frame_audit"],
        }
        print(
            json.dumps(
                summary,
                sort_keys=True,
                indent=2 if args.pretty else None,
                separators=None if args.pretty else (",", ":"),
                allow_nan=False,
            )
        )
        return 0
    except CacheScreenError as exc:
        print(f"cache screen rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
