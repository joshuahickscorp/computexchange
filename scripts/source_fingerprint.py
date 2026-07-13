#!/usr/bin/env python3
"""Fingerprint the exact non-ignored source tree used by a proof run.

The commit SHA alone is insufficient when a proof runs from a dirty worktree.
This tool hashes HEAD plus every tracked or non-ignored untracked path, including
its executable bit and content (or symlink target), without printing file data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Any


SCHEMA_VERSION = 1
DOMAIN = b"computexchange-source-fingerprint-v1\0"


class FingerprintError(RuntimeError):
    """The source tree could not be enumerated or hashed safely."""


def _git(root: Path, *args: str) -> bytes:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), *args], stderr=subprocess.PIPE
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError):
            detail = exc.stderr.decode("utf-8", "replace").strip()
        raise FingerprintError(detail or f"git {' '.join(args)} failed") from exc


def _framed(hasher: Any, value: bytes) -> None:
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _hash_file(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.digest()


def source_fingerprint(root: Path | str) -> dict[str, Any]:
    requested = Path(root).resolve()
    repo = Path(_git(requested, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    try:
        head = _git(repo, "rev-parse", "HEAD").decode("ascii").strip()
    except FingerprintError:
        head = "UNBORN"

    raw_paths = _git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    paths = sorted({part for part in raw_paths.split(b"\0") if part})
    raw_status = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")

    digest = hashlib.sha256()
    digest.update(DOMAIN)
    _framed(digest, head.encode("ascii"))
    for raw_path in paths:
        relative = raw_path.decode("utf-8", "surrogateescape")
        path = repo / relative
        _framed(digest, raw_path)
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            _framed(digest, b"missing")
            continue
        if stat.S_ISLNK(mode):
            _framed(digest, b"symlink")
            _framed(digest, os.readlink(path).encode("utf-8", "surrogateescape"))
        elif stat.S_ISREG(mode):
            _framed(digest, b"file+x" if mode & stat.S_IXUSR else b"file")
            _framed(digest, _hash_file(path))
        elif stat.S_ISDIR(mode):
            # A directory returned by git is normally a gitlink/submodule. Record
            # its indexed identity without walking ignored dependency contents.
            _framed(digest, b"gitlink")
            _framed(digest, _git(repo, "rev-parse", f"HEAD:{relative}"))
        else:
            raise FingerprintError(f"unsupported source path type: {relative}")

    return {
        "schema_version": SCHEMA_VERSION,
        "head": head,
        "dirty": bool(raw_status),
        "file_count": len(paths),
        "status_sha256": hashlib.sha256(raw_status).hexdigest(),
        "source_sha256": digest.hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument(
        "--field",
        choices=("head", "dirty", "file_count", "status_sha256", "source_sha256"),
        help="print one field instead of the canonical JSON object",
    )
    args = parser.parse_args()
    try:
        result = source_fingerprint(args.root)
    except FingerprintError as exc:
        parser.exit(2, f"source-fingerprint: {exc}\n")
    if args.field:
        value = result[args.field]
        print(str(value).lower() if isinstance(value, bool) else value)
    else:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
