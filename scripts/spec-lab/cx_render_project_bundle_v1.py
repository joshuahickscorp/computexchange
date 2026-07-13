#!/usr/bin/env python3
"""Create and verify a strict content manifest for a Blender project bundle.

This is an additive lab/preflight boundary.  It does not admit a production job,
make a render billable, or claim that a project is renderable.  It gives the
future render job contract one deterministic identity for the entry ``.blend``
and every opaque regular file shipped beside it.

The manifest deliberately lives outside the project root.  Every file type is
treated as bytes; Blender compatibility is a separate, pinned-Blender preflight.
Symlinks, hard links, devices, sockets, FIFOs, path traversal, non-portable path
aliases, mutation races, and unbounded bundles fail closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import stat
import sys
import unicodedata
from typing import Any


SCHEMA_VERSION = 2
MANIFEST_KIND = "cx_render_project_bundle_manifest"
MAX_FILES = 4_096
MAX_DIRECTORIES = 4_096
MAX_DEPTH = 64
MAX_TOTAL_BYTES = 2 << 30
MAX_FILE_BYTES = 2 << 30
MAX_RELATIVE_PATH_BYTES = 1_024
MAX_MANIFEST_BYTES = 8 << 20

_MANIFEST_KEYS = {
    "schema_version",
    "kind",
    "scene_path",
    "scene_sha256",
    "bundle_sha256",
    "file_count",
    "directory_count",
    "total_bytes",
    "entries",
    "directories",
    "manifest_sha256",
}
_ENTRY_KEYS = {"path", "bytes", "sha256"}
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class ProjectBundleError(ValueError):
    """A deterministic project-bundle contract violation."""


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProjectBundleError("manifest is not canonical finite JSON") from exc


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


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


def _directory_identity(path: Path) -> tuple[int, ...]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ProjectBundleError(f"cannot inspect project directory {path}: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise ProjectBundleError(f"project directory is not a non-symlink directory: {path}")
    return _identity(info)


def _strict_component(component: str) -> None:
    if (
        not component
        or component in {".", ".."}
        or "\\" in component
        or ":" in component
        or component.endswith((" ", "."))
        or unicodedata.normalize("NFC", component) != component
        or any(unicodedata.category(character).startswith("C") for character in component)
    ):
        raise ProjectBundleError(f"non-portable project path component {component!r}")
    stem = component.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED:
        raise ProjectBundleError(f"reserved project path component {component!r}")


def strict_relative_path(value: Any, *, require_blend: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise ProjectBundleError("project path must be a nonempty string")
    if "\x00" in value or value.startswith("/"):
        raise ProjectBundleError("project path must be strict relative POSIX")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProjectBundleError("project path must be valid UTF-8") from exc
    if len(encoded) > MAX_RELATIVE_PATH_BYTES:
        raise ProjectBundleError(
            f"project path exceeds {MAX_RELATIVE_PATH_BYTES} UTF-8 bytes"
        )
    raw_parts = value.split("/")
    if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
        raise ProjectBundleError("project path has an empty, dot, or parent component")
    if len(raw_parts) > MAX_DEPTH:
        raise ProjectBundleError(f"project path exceeds {MAX_DEPTH} components")
    for component in raw_parts:
        _strict_component(component)
    normalized = PurePosixPath(*raw_parts).as_posix()
    if normalized != value:
        raise ProjectBundleError("project path is not canonical POSIX")
    if require_blend and not value.endswith(".blend"):
        raise ProjectBundleError("scene_path must end in lowercase .blend")
    return value


def _project_root(raw: Path | str) -> Path:
    root = Path(os.path.abspath(os.fspath(raw)))
    _directory_identity(root)
    return root


def _walk_project(root: Path) -> tuple[list[tuple[Path, str]], dict[Path, tuple[int, ...]]]:
    files: list[tuple[Path, str]] = []
    directories: dict[Path, tuple[int, ...]] = {root: _directory_identity(root)}

    def walk(directory: Path, parts: tuple[str, ...]) -> None:
        try:
            rows = sorted(os.scandir(directory), key=lambda row: row.name.encode("utf-8"))
        except (OSError, UnicodeEncodeError) as exc:
            raise ProjectBundleError(
                f"cannot enumerate project directory {directory}: {exc}"
            ) from exc
        for row in rows:
            _strict_component(row.name)
            relative_parts = (*parts, row.name)
            if len(relative_parts) > MAX_DEPTH:
                raise ProjectBundleError(f"project path exceeds {MAX_DEPTH} components")
            relative = PurePosixPath(*relative_parts).as_posix()
            strict_relative_path(relative)
            path = directory / row.name
            try:
                info = row.stat(follow_symlinks=False)
            except OSError as exc:
                raise ProjectBundleError(f"cannot inspect project entry {relative}: {exc}") from exc
            if stat.S_ISLNK(info.st_mode):
                raise ProjectBundleError(f"project bundle cannot contain symlink {relative!r}")
            if stat.S_ISDIR(info.st_mode):
                if len(directories) >= MAX_DIRECTORIES:
                    raise ProjectBundleError(
                        f"project bundle exceeds {MAX_DIRECTORIES} directories"
                    )
                directories[path] = _identity(info)
                walk(path, relative_parts)
            elif stat.S_ISREG(info.st_mode):
                files.append((path, relative))
                if len(files) > MAX_FILES:
                    raise ProjectBundleError(f"project bundle exceeds {MAX_FILES} files")
            else:
                raise ProjectBundleError(
                    f"project bundle contains non-regular entry {relative!r}"
                )

    walk(root, ())
    return files, directories


def _snapshot_file(
    path: Path, relative: str, *, scene: bool
) -> tuple[dict[str, Any], tuple[int, ...]]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ProjectBundleError(f"cannot open project file {relative!r}: {exc}") from exc
    digest = hashlib.sha256()
    total = 0
    prefix = bytearray()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ProjectBundleError(f"project file changed type: {relative!r}")
        if before.st_nlink != 1:
            raise ProjectBundleError(f"project file must not be hard-linked: {relative!r}")
        if before.st_size > MAX_FILE_BYTES:
            raise ProjectBundleError(
                f"project file {relative!r} exceeds {MAX_FILE_BYTES} bytes"
            )
        while True:
            chunk = os.read(descriptor, 1 << 20)
            if not chunk:
                break
            if len(prefix) < 12:
                prefix.extend(chunk[: 12 - len(prefix)])
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise ProjectBundleError(
                    f"project file {relative!r} exceeds {MAX_FILE_BYTES} bytes"
                )
            digest.update(chunk)
        after = os.fstat(descriptor)
        identity = _identity(after)
        if _identity(before) != identity or total != after.st_size:
            raise ProjectBundleError(f"project file changed while hashed: {relative!r}")
    finally:
        os.close(descriptor)
    try:
        current = path.lstat()
    except OSError as exc:
        raise ProjectBundleError(f"project file disappeared after hashing: {relative!r}") from exc
    if not stat.S_ISREG(current.st_mode) or path.is_symlink() or _identity(current) != identity:
        raise ProjectBundleError(f"project file path changed after hashing: {relative!r}")
    if scene and not bytes(prefix).startswith(b"BLENDER"):
        raise ProjectBundleError("scene_path does not have a Blender file header")
    return (
        {"path": relative, "bytes": total, "sha256": digest.hexdigest()},
        identity,
    )


def _bundle_sha256(entries: list[dict[str, Any]], directories: list[str]) -> str:
    digest = hashlib.sha256()
    for directory in directories:
        relative = directory.encode("utf-8")
        digest.update(b"D")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
    for entry in entries:
        relative = entry["path"].encode("utf-8")
        digest.update(b"F")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(entry["bytes"].to_bytes(8, "big"))
        digest.update(bytes.fromhex(entry["sha256"]))
    return digest.hexdigest()


def _manifest_sha256(manifest: dict[str, Any]) -> str:
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    return hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def build_manifest(root: Path | str, scene_path: str) -> dict[str, Any]:
    """Hash one bounded, symlink-free project snapshot deterministically."""
    project_root = _project_root(root)
    scene_path = strict_relative_path(scene_path, require_blend=True)
    files, directories = _walk_project(project_root)
    relative_names = [relative for _, relative in files]
    directory_names = sorted(
        (
            PurePosixPath(*path.relative_to(project_root).parts).as_posix()
            for path in directories
            if path != project_root
        ),
        key=lambda value: value.encode("utf-8"),
    )
    folded: dict[str, str] = {}
    for relative in [*directory_names, *relative_names]:
        alias = unicodedata.normalize("NFC", relative).casefold()
        prior = folded.get(alias)
        if prior is not None and prior != relative:
            raise ProjectBundleError(
                f"project paths collide on a case-insensitive filesystem: {prior!r}, {relative!r}"
            )
        folded[alias] = relative
    if scene_path not in set(relative_names):
        raise ProjectBundleError(f"scene_path is absent from project bundle: {scene_path!r}")

    entries: list[dict[str, Any]] = []
    file_identities: dict[Path, tuple[int, ...]] = {}
    total_bytes = 0
    for path, relative in files:
        entry, identity = _snapshot_file(
            path, relative, scene=relative == scene_path
        )
        file_identities[path] = identity
        total_bytes += entry["bytes"]
        if total_bytes > MAX_TOTAL_BYTES:
            raise ProjectBundleError(
                f"project bundle exceeds {MAX_TOTAL_BYTES} total bytes"
            )
        entries.append(entry)
    entries.sort(key=lambda entry: entry["path"].encode("utf-8"))

    # Directory identity catches path swaps/add/remove races during the file pass.
    for directory, expected in directories.items():
        if _directory_identity(directory) != expected:
            raise ProjectBundleError("project directory changed during manifest creation")
    after_files, after_directories = _walk_project(project_root)
    if [relative for _, relative in after_files] != relative_names:
        raise ProjectBundleError("project file set changed during manifest creation")
    if set(after_directories) != set(directories):
        raise ProjectBundleError("project directory set changed during manifest creation")
    after_directory_names = sorted(
        (
            PurePosixPath(*path.relative_to(project_root).parts).as_posix()
            for path in after_directories
            if path != project_root
        ),
        key=lambda value: value.encode("utf-8"),
    )
    if after_directory_names != directory_names:
        raise ProjectBundleError("project directory names changed during manifest creation")
    # A file hashed early can change while a later file is being read without
    # touching its parent directory metadata. Rebind every path identity after
    # the complete file pass so such stale snapshots fail closed.
    for path, expected in file_identities.items():
        try:
            current = path.lstat()
        except OSError as exc:
            raise ProjectBundleError("project file disappeared after the hash pass") from exc
        if (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or _identity(current) != expected
        ):
            raise ProjectBundleError("project file changed after its hash pass")

    scene_entry = next(entry for entry in entries if entry["path"] == scene_path)
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "scene_path": scene_path,
        "scene_sha256": scene_entry["sha256"],
        "bundle_sha256": _bundle_sha256(entries, directory_names),
        "file_count": len(entries),
        "directory_count": len(directory_names),
        "total_bytes": total_bytes,
        "entries": entries,
        "directories": directory_names,
    }
    manifest["manifest_sha256"] = _manifest_sha256(manifest)
    if len(_canonical_json(manifest)) > MAX_MANIFEST_BYTES:
        raise ProjectBundleError(f"project manifest exceeds {MAX_MANIFEST_BYTES} bytes")
    return manifest


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in pairs:
        if key in value:
            raise ProjectBundleError(f"duplicate manifest JSON key {key!r}")
        value[key] = child
    return value


def parse_manifest(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_MANIFEST_BYTES:
        raise ProjectBundleError(
            f"manifest must contain 1..{MAX_MANIFEST_BYTES} bytes"
        )
    try:
        manifest = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ProjectBundleError(f"invalid JSON constant {value}")
            ),
        )
    except ProjectBundleError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ProjectBundleError("manifest is not strict UTF-8 JSON") from exc
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS:
        raise ProjectBundleError("manifest has an unknown or missing top-level field")
    if (
        type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != SCHEMA_VERSION
        or not isinstance(manifest["kind"], str)
        or manifest["kind"] != MANIFEST_KIND
    ):
        raise ProjectBundleError("manifest schema_version/kind mismatch")
    scene_path = strict_relative_path(manifest["scene_path"], require_blend=True)
    for field in ("scene_sha256", "bundle_sha256", "manifest_sha256"):
        if not _is_sha256(manifest[field]):
            raise ProjectBundleError(f"{field} must be lowercase SHA-256")
    if type(manifest["file_count"]) is not int or not 1 <= manifest["file_count"] <= MAX_FILES:
        raise ProjectBundleError("manifest file_count is outside its fixed bound")
    if (
        type(manifest["directory_count"]) is not int
        or not 0 <= manifest["directory_count"] < MAX_DIRECTORIES
    ):
        raise ProjectBundleError("manifest directory_count is outside its fixed bound")
    if (
        type(manifest["total_bytes"]) is not int
        or not 0 <= manifest["total_bytes"] <= MAX_TOTAL_BYTES
    ):
        raise ProjectBundleError("manifest total_bytes is outside its fixed bound")
    entries = manifest["entries"]
    if not isinstance(entries, list) or len(entries) != manifest["file_count"]:
        raise ProjectBundleError("manifest entries contradict file_count")
    paths: list[str] = []
    total_bytes = 0
    folded: dict[str, str] = {}
    directory_rows = manifest["directories"]
    if (
        not isinstance(directory_rows, list)
        or len(directory_rows) != manifest["directory_count"]
    ):
        raise ProjectBundleError("manifest directories contradict directory_count")
    directory_paths: list[str] = []
    for index, value in enumerate(directory_rows):
        path = strict_relative_path(value)
        alias = unicodedata.normalize("NFC", path).casefold()
        if path in directory_paths or alias in folded:
            raise ProjectBundleError(
                f"manifest directory {index} duplicates or aliases another path"
            )
        folded[alias] = path
        directory_paths.append(path)
    if directory_paths != sorted(
        directory_paths, key=lambda path: path.encode("utf-8")
    ):
        raise ProjectBundleError("manifest directories are not in canonical path order")
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
            raise ProjectBundleError(f"manifest entry {index} has an unknown or missing field")
        path = strict_relative_path(entry["path"])
        if type(entry["bytes"]) is not int or not 0 <= entry["bytes"] <= MAX_FILE_BYTES:
            raise ProjectBundleError(f"manifest entry {index} has an invalid byte count")
        if not _is_sha256(entry["sha256"]):
            raise ProjectBundleError(f"manifest entry {index} has an invalid SHA-256")
        alias = unicodedata.normalize("NFC", path).casefold()
        if path in paths or alias in folded:
            raise ProjectBundleError("manifest contains duplicate or cross-platform alias paths")
        folded[alias] = path
        paths.append(path)
        total_bytes += entry["bytes"]
        if total_bytes > MAX_TOTAL_BYTES:
            raise ProjectBundleError("manifest entries exceed the total byte bound")
    if paths != sorted(paths, key=lambda path: path.encode("utf-8")):
        raise ProjectBundleError("manifest entries are not in canonical path order")
    if scene_path not in paths:
        raise ProjectBundleError("manifest scene_path has no matching entry")
    scene_entry = entries[paths.index(scene_path)]
    if scene_entry["sha256"] != manifest["scene_sha256"]:
        raise ProjectBundleError("manifest scene SHA contradicts its entry")
    if total_bytes != manifest["total_bytes"]:
        raise ProjectBundleError("manifest entries contradict total_bytes")
    if _bundle_sha256(entries, directory_paths) != manifest["bundle_sha256"]:
        raise ProjectBundleError("manifest bundle SHA is invalid")
    if _manifest_sha256(manifest) != manifest["manifest_sha256"]:
        raise ProjectBundleError("manifest self SHA is invalid")
    return manifest


def verify_manifest(root: Path | str, manifest: dict[str, Any] | bytes) -> dict[str, Any]:
    expected = parse_manifest(manifest) if isinstance(manifest, bytes) else parse_manifest(
        _canonical_json(manifest)
    )
    actual = build_manifest(root, expected["scene_path"])
    if actual != expected:
        raise ProjectBundleError("project bytes do not match the content manifest")
    return actual


def _read_bounded_file(path: Path, maximum: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ProjectBundleError(f"cannot open manifest {path}: {exc}") from exc
    data = bytearray()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise ProjectBundleError("manifest path must name a bounded regular file")
        while len(data) <= maximum:
            chunk = os.read(descriptor, min(1 << 20, maximum + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        if _identity(before) != _identity(after) or len(data) != after.st_size:
            raise ProjectBundleError("manifest file changed while read")
    finally:
        os.close(descriptor)
    if len(data) > maximum:
        raise ProjectBundleError(f"manifest exceeds {maximum} bytes")
    return bytes(data)


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _publish_new(path: Path, payload: bytes, project_root: Path) -> None:
    try:
        parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise ProjectBundleError(f"manifest output parent is unavailable: {exc}") from exc
    destination = parent / path.name
    if not path.name or path.name in {".", ".."}:
        raise ProjectBundleError("manifest output must name a file")
    if _is_within(destination, project_root.resolve(strict=True)):
        raise ProjectBundleError("manifest output must live outside the project root")
    if destination.exists() or destination.is_symlink():
        raise ProjectBundleError(f"refusing to replace existing manifest {destination}")
    temporary = parent / f".{path.name}.{secrets.token_hex(16)}.tmp"
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    linked = False
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise ProjectBundleError("manifest publication made no write progress")
            offset += written
        os.fsync(descriptor)
        os.link(temporary, destination, follow_symlinks=False)
        linked = True
        published = destination.lstat()
        if _identity(published) != _identity(os.fstat(descriptor)):
            raise ProjectBundleError("published manifest path changed identity")
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
            if _identity(destination.lstat()) != _identity(os.fstat(descriptor)):
                raise ProjectBundleError("published manifest changed during directory sync")
            os.lseek(descriptor, 0, os.SEEK_SET)
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(descriptor, 1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                digest.update(chunk)
            if total != len(payload) or digest.digest() != hashlib.sha256(payload).digest():
                raise ProjectBundleError("published manifest bytes changed after sync")
        finally:
            os.close(directory_fd)
    except BaseException:
        if linked:
            try:
                destination.unlink()
            except OSError:
                pass
        raise
    finally:
        os.close(descriptor)
        try:
            temporary.unlink()
        except OSError:
            pass
    directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _manifest_path_outside_root(path: Path, root: Path) -> Path:
    candidate = Path(os.path.abspath(os.fspath(path)))
    try:
        parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise ProjectBundleError(f"manifest parent is unavailable: {exc}") from exc
    resolved = parent / candidate.name
    if _is_within(resolved, root.resolve(strict=True)):
        raise ProjectBundleError("manifest file must live outside the project root")
    return resolved


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create", help="create a deterministic bundle manifest")
    create.add_argument("--root", required=True, type=Path)
    create.add_argument("--scene", required=True)
    create.add_argument("--output", type=Path, help="new file outside root; default stdout")
    verify = subparsers.add_parser("verify", help="verify project bytes against a manifest")
    verify.add_argument("--root", required=True, type=Path)
    verify.add_argument("--manifest", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = _project_root(args.root)
        if args.command == "create":
            manifest = build_manifest(root, args.scene)
            encoded = _canonical_json(manifest) + b"\n"
            if args.output is None:
                sys.stdout.buffer.write(encoded)
                sys.stdout.buffer.flush()
            else:
                _publish_new(args.output, encoded, root)
            return 0
        manifest_path = _manifest_path_outside_root(args.manifest, root)
        raw = _read_bounded_file(manifest_path, MAX_MANIFEST_BYTES)
        verified = verify_manifest(root, raw)
        result = {
            "ok": True,
            "kind": MANIFEST_KIND,
            "bundle_sha256": verified["bundle_sha256"],
            "manifest_sha256": verified["manifest_sha256"],
            "file_count": verified["file_count"],
            "total_bytes": verified["total_bytes"],
        }
        sys.stdout.buffer.write(_canonical_json(result) + b"\n")
        sys.stdout.buffer.flush()
        return 0
    except ProjectBundleError as exc:
        print(f"project bundle rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
