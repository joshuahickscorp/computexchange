#!/usr/bin/env python3
"""Launch the isolated vLLM anchor only after its complete identity is verified."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from validate_runtime_lock import (
    EXACT_RUNTIME,
    LockValidationError,
    load_and_validate_lock,
)


class LauncherError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise LauncherError(f"cannot hash model artifact {path}: {exc}") from exc
    return digest.hexdigest()


def verify_artifact(lock: Mapping[str, Any], model_dir: Path) -> Path:
    try:
        resolved_dir = model_dir.resolve(strict=True)
    except OSError as exc:
        raise LauncherError(f"model directory does not exist: {model_dir}: {exc}") from exc
    if not resolved_dir.is_dir():
        raise LauncherError(f"model directory is not a directory: {resolved_dir}")

    artifact = resolved_dir / lock["model"]["artifact_filename"]
    try:
        resolved_artifact = artifact.resolve(strict=True)
    except OSError as exc:
        raise LauncherError(f"model artifact does not exist: {artifact}: {exc}") from exc
    try:
        resolved_artifact.relative_to(resolved_dir)
    except ValueError as exc:
        raise LauncherError(
            f"model artifact resolves outside model directory: {resolved_artifact}"
        ) from exc
    if not resolved_artifact.is_file():
        raise LauncherError(f"model artifact is not a regular file: {resolved_artifact}")
    if resolved_artifact.stat().st_size == 0:
        raise LauncherError(f"model artifact is empty: {resolved_artifact}")

    actual_digest = sha256_file(resolved_artifact)
    expected_digest = lock["model"]["artifact_sha256"]
    if actual_digest != expected_digest:
        raise LauncherError(
            "model artifact SHA-256 mismatch: "
            f"expected {expected_digest}, got {actual_digest}"
        )
    return resolved_artifact


def enforce_mode(lock: Mapping[str, Any], mode: str) -> None:
    status = lock["status"]
    if mode == "production" and status != "production":
        raise LauncherError(
            f"refusing production launch: runtime-lock status is {status!r}, not 'production'"
        )


def verify_runtime_identity(environment: Mapping[str, str] = os.environ) -> None:
    try:
        installed_version = importlib.metadata.version("vllm")
    except importlib.metadata.PackageNotFoundError as exc:
        raise LauncherError("vLLM is not installed in this Python environment") from exc
    if installed_version != EXACT_RUNTIME["vllm_version"]:
        raise LauncherError(
            "installed vLLM version mismatch: "
            f"expected {EXACT_RUNTIME['vllm_version']}, got {installed_version}"
        )

    build_commit = environment.get("VLLM_BUILD_COMMIT")
    if build_commit != EXACT_RUNTIME["vllm_commit"]:
        raise LauncherError(
            "VLLM_BUILD_COMMIT mismatch or missing: "
            f"expected {EXACT_RUNTIME['vllm_commit']}, got {build_commit!r}"
        )

    image_digest = environment.get("CX_VLLM_IMAGE_DIGEST")
    expected_digest = EXACT_RUNTIME["container_image"].rsplit("@", 1)[1]
    if image_digest != expected_digest:
        raise LauncherError(
            "CX_VLLM_IMAGE_DIGEST mismatch or missing: "
            f"expected {expected_digest}, got {image_digest!r}"
        )


def _compact_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def build_command(
    lock: Mapping[str, Any], artifact: Path, api_key: str | None = None
) -> list[str]:
    model = lock["model"]
    execution = lock["execution"]
    attention = execution["attention"]
    compilation = execution["compilation"]
    network = execution["network"]
    speculative = dict(lock["speculative_decoding"])
    speculative.pop("enabled")

    command = [
        "vllm",
        "serve",
        str(artifact),
        "--served-model-name",
        model["served_model_name"],
        "--tokenizer",
        model["tokenizer_repository"],
        "--tokenizer-revision",
        model["tokenizer_revision"],
        "--dtype",
        execution["dtype"],
        "--load-format",
        execution["weight_format"],
        "--kv-cache-dtype",
        execution["resolved_kv_cache_dtype"],
        "--tensor-parallel-size",
        str(execution["tensor_parallel_size"]),
        "--pipeline-parallel-size",
        str(execution["pipeline_parallel_size"]),
        "--data-parallel-size",
        str(execution["data_parallel_size"]),
        "--max-model-len",
        str(execution["max_model_len"]),
        "--max-num-seqs",
        str(execution["max_num_seqs"]),
        "--max-num-batched-tokens",
        str(execution["max_num_batched_tokens"]),
        "--gpu-memory-utilization",
        str(execution["gpu_memory_utilization"]),
        "--seed",
        str(execution["seed"]),
        "--attention-config",
        _compact_json(attention),
        "--compilation-config",
        _compact_json(compilation),
        "--speculative-config",
        _compact_json(speculative),
        "--host",
        network["host"],
        "--port",
        str(network["port"]),
    ]
    if api_key:
        command.extend(["--api-key", api_key])
    return command


def _redacted_command(command: Sequence[str]) -> str:
    redacted = list(command)
    try:
        key_index = redacted.index("--api-key") + 1
    except ValueError:
        pass
    else:
        if key_index < len(redacted):
            redacted[key_index] = "<redacted>"
    return shlex.join(redacted)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True, help="validated lock JSON")
    parser.add_argument(
        "--model-dir",
        type=Path,
        required=True,
        help="directory containing the exact GGUF named in the lock",
    )
    parser.add_argument(
        "--mode", choices=("soak", "production"), default="soak", help="launch gate"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="verify and print without exec"
    )
    parser.add_argument(
        "--skip-runtime-checks",
        action="store_true",
        help="skip installed-package/container checks; allowed only for soak dry-runs",
    )
    args = parser.parse_args(argv)

    try:
        lock = load_and_validate_lock(args.lock)
        enforce_mode(lock, args.mode)
        if args.skip_runtime_checks and not (args.dry_run and args.mode == "soak"):
            raise LauncherError(
                "--skip-runtime-checks is permitted only with --dry-run --mode soak"
            )
        artifact = verify_artifact(lock, args.model_dir)
        if not args.skip_runtime_checks:
            verify_runtime_identity()
        api_key = os.environ.get("CX_VLLM_API_KEY")
        if args.mode == "production" and not api_key:
            raise LauncherError("CX_VLLM_API_KEY is required for production")
        command = build_command(lock, artifact, api_key)
    except (LockValidationError, LauncherError) as exc:
        print(f"vLLM launch refused: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(_redacted_command(command))
        return 0
    os.execvp(command[0], command)
    raise AssertionError("os.execvp unexpectedly returned")


if __name__ == "__main__":
    raise SystemExit(main())
