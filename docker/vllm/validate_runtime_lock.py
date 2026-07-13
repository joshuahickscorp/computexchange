#!/usr/bin/env python3
"""Fail-closed validation for the isolated vLLM runtime lock.

This intentionally uses only the Python standard library so it can run before
the vLLM environment (or any project dependency) is installed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


MAX_LOCK_BYTES = 64 * 1024

EXACT_RUNTIME = {
    "vllm_version": "0.24.0",
    "vllm_commit": "ee0da84ab9e04ac7610e28580af62c365e898389",
    "container_image": (
        "vllm/vllm-openai@sha256:"
        "f9de5cd9fa907fbf6dbba691eb7db095d48ad58ea283e3eba7142f9a91e186e8"
    ),
    "container_index_digest": (
        "sha256:251eba5cc7c12fed0b75da22a9240e582b1c9e39f6fbc064f86781b963bd814f"
    ),
    "container_platform": "linux/amd64",
    "wheel_sha256": (
        "2d2831aeba311292250df0132dbc4d8e9f42c654536eaec48e6fe58acb1822cf"
    ),
    "source_sdist_sha256": (
        "0862453adc1f3339f1a0c9dca1179c34d6ed6e118f87b6e5bddd120af614ac66"
    ),
    "cuda_runtime": "13.0.2",
    "torch_version": "2.11.0",
}

TOP_LEVEL_KEYS = {
    "schema_version",
    "status",
    "runtime",
    "model",
    "execution",
    "speculative_decoding",
    "sampling",
    "canary",
}
MODEL_KEYS = {
    "catalog_id",
    "repository",
    "revision",
    "artifact_filename",
    "artifact_sha256",
    "tokenizer_repository",
    "tokenizer_revision",
    "served_model_name",
}
EXECUTION_KEYS = {
    "weight_format",
    "quantization",
    "dtype",
    "tensor_parallel_size",
    "pipeline_parallel_size",
    "data_parallel_size",
    "resolved_kv_cache_dtype",
    "max_model_len",
    "max_num_seqs",
    "max_num_batched_tokens",
    "gpu_memory_utilization",
    "seed",
    "trust_remote_code",
    "attention",
    "compilation",
    "network",
}
SAMPLING_KEYS = {
    "temperature",
    "top_p",
    "top_k",
    "seed",
    "n",
    "presence_penalty",
    "frequency_penalty",
}
CANARY_KEYS = {
    "prompt",
    "prompt_sha256",
    "max_tokens",
    "expected_completion_sha256",
    "warmup_requests",
    "measured_requests",
    "minimum_acceptance_rate",
    "minimum_output_match_rate",
}

HEX_40 = re.compile(r"^[0-9a-f]{40}$")
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
GGUF_BASENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*\.gguf$")
REPOSITORY = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
ATTENTION_BACKENDS = {"FLASH_ATTN", "FLASHINFER", "TRITON_ATTN"}

PLACEHOLDER_MARKERS = (
    "REQUIRED",
    "PLACEHOLDER",
    "CHANGEME",
    "UNKNOWN",
    "TODO",
    "TBD",
)
FLOATING_IDENTITIES = {
    "auto",
    "default",
    "head",
    "latest",
    "main",
    "master",
    "nightly",
    "none",
    "stable",
}


class LockValidationError(ValueError):
    """One or more lock validation failures."""

    def __init__(self, errors: str | Iterable[str]):
        if isinstance(errors, str):
            self.errors = (errors,)
        else:
            self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


def _reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LockValidationError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> None:
    raise LockValidationError(f"non-finite JSON number is forbidden: {value}")


def load_lock(path: str | Path) -> dict[str, Any]:
    """Load a small UTF-8 JSON lock while rejecting duplicates and NaN values."""

    lock_path = Path(path)
    try:
        raw = lock_path.read_bytes()
    except OSError as exc:
        raise LockValidationError(f"cannot read {lock_path}: {exc}") from exc
    if not raw:
        raise LockValidationError(f"{lock_path} is empty")
    if len(raw) > MAX_LOCK_BYTES:
        raise LockValidationError(
            f"{lock_path} is {len(raw)} bytes; maximum is {MAX_LOCK_BYTES}"
        )
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LockValidationError(f"{lock_path} is not UTF-8: {exc}") from exc
    try:
        value = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_json,
        )
    except LockValidationError:
        raise
    except json.JSONDecodeError as exc:
        raise LockValidationError(f"invalid JSON in {lock_path}: {exc}") from exc
    if type(value) is not dict:
        raise LockValidationError("runtime lock root must be a JSON object")
    return value


def _is_placeholder(value: str) -> bool:
    upper = value.upper()
    return any(marker in upper for marker in PLACEHOLDER_MARKERS)


class _Validator:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def error(self, path: str, message: str) -> None:
        self.errors.append(f"{path}: {message}")

    def object(
        self,
        path: str,
        value: Any,
        required: set[str],
        allowed: set[str] | None = None,
    ) -> Mapping[str, Any]:
        if type(value) is not dict:
            self.error(path, "must be an object")
            return {}
        actual = set(value)
        for key in sorted(required - actual):
            self.error(path, f"missing required field {key!r}")
        for key in sorted(actual - (allowed or required)):
            self.error(path, f"unknown field {key!r}")
        return value

    def exact(self, path: str, value: Any, expected: Any) -> None:
        if type(value) is not type(expected) or value != expected:
            self.error(path, f"must equal {expected!r}")

    def one_of(self, path: str, value: Any, allowed: set[Any]) -> None:
        if not any(type(value) is type(candidate) and value == candidate for candidate in allowed):
            self.error(path, f"must be one of {sorted(allowed)!r}")

    def string(
        self,
        path: str,
        value: Any,
        *,
        pattern: re.Pattern[str] | None = None,
        max_length: int = 4096,
        reject_floating: bool = True,
    ) -> str | None:
        if type(value) is not str:
            self.error(path, "must be a string")
            return None
        if not value:
            self.error(path, "must not be empty")
            return None
        if len(value) > max_length:
            self.error(path, f"must be at most {max_length} characters")
        if _is_placeholder(value):
            self.error(path, "contains an unresolved placeholder")
        if reject_floating and value.casefold() in FLOATING_IDENTITIES:
            self.error(path, f"floating identity {value!r} is forbidden")
        if pattern is not None and pattern.fullmatch(value) is None:
            self.error(path, f"does not match {pattern.pattern!r}")
        return value

    def integer(
        self, path: str, value: Any, minimum: int, maximum: int
    ) -> int | None:
        if type(value) is not int:
            self.error(path, "must be an integer")
            return None
        if not minimum <= value <= maximum:
            self.error(path, f"must be between {minimum} and {maximum}")
        return value

    def number(
        self,
        path: str,
        value: Any,
        minimum: float,
        maximum: float,
        *,
        exclusive_minimum: bool = False,
    ) -> float | None:
        if type(value) not in (int, float):
            self.error(path, "must be a finite number")
            return None
        numeric = float(value)
        if not math.isfinite(numeric):
            self.error(path, "must be a finite number")
            return None
        lower_ok = numeric > minimum if exclusive_minimum else numeric >= minimum
        if not lower_ok or numeric > maximum:
            qualifier = "greater than" if exclusive_minimum else "at least"
            self.error(path, f"must be {qualifier} {minimum} and at most {maximum}")
        return numeric

    def sha256(self, path: str, value: Any) -> str | None:
        result = self.string(path, value, pattern=HEX_64, max_length=64)
        if result is not None and result in {"0" * 64, "f" * 64}:
            self.error(path, "sentinel digest is forbidden")
        return result


def collect_validation_errors(lock: Any) -> list[str]:
    """Return every discovered lock error. An empty list means valid."""

    v = _Validator()
    root = v.object("$", lock, TOP_LEVEL_KEYS)

    v.exact("$.schema_version", root.get("schema_version"), 1)
    v.one_of("$.status", root.get("status"), {"candidate", "production"})

    runtime = v.object(
        "$.runtime", root.get("runtime"), set(EXACT_RUNTIME), set(EXACT_RUNTIME)
    )
    for key, expected in EXACT_RUNTIME.items():
        v.exact(f"$.runtime.{key}", runtime.get(key), expected)

    model = v.object("$.model", root.get("model"), MODEL_KEYS)
    v.exact(
        "$.model.catalog_id",
        model.get("catalog_id"),
        "llama-3.2-1b-instruct-q4",
    )
    v.exact(
        "$.model.repository",
        model.get("repository"),
        "unsloth/Llama-3.2-1B-Instruct-GGUF",
    )
    v.string("$.model.revision", model.get("revision"), pattern=HEX_40, max_length=40)
    v.string(
        "$.model.artifact_filename",
        model.get("artifact_filename"),
        pattern=GGUF_BASENAME,
        max_length=255,
    )
    v.sha256("$.model.artifact_sha256", model.get("artifact_sha256"))
    v.string(
        "$.model.tokenizer_repository",
        model.get("tokenizer_repository"),
        pattern=REPOSITORY,
        max_length=255,
    )
    v.string(
        "$.model.tokenizer_revision",
        model.get("tokenizer_revision"),
        pattern=HEX_40,
        max_length=40,
    )
    v.exact(
        "$.model.served_model_name",
        model.get("served_model_name"),
        "llama-3.2-1b-instruct-q4",
    )

    execution = v.object("$.execution", root.get("execution"), EXECUTION_KEYS)
    v.exact("$.execution.weight_format", execution.get("weight_format"), "gguf")
    v.exact("$.execution.quantization", execution.get("quantization"), "q4_k_m")
    v.exact("$.execution.dtype", execution.get("dtype"), "float16")
    for key in (
        "tensor_parallel_size",
        "pipeline_parallel_size",
        "data_parallel_size",
    ):
        v.integer(f"$.execution.{key}", execution.get(key), 1, 64)
    v.one_of(
        "$.execution.resolved_kv_cache_dtype",
        execution.get("resolved_kv_cache_dtype"),
        {"float16", "bfloat16", "fp8_e4m3", "fp8_e5m2"},
    )
    max_model_len = v.integer(
        "$.execution.max_model_len", execution.get("max_model_len"), 128, 131072
    )
    v.integer("$.execution.max_num_seqs", execution.get("max_num_seqs"), 1, 4096)
    v.integer(
        "$.execution.max_num_batched_tokens",
        execution.get("max_num_batched_tokens"),
        128,
        1048576,
    )
    v.number(
        "$.execution.gpu_memory_utilization",
        execution.get("gpu_memory_utilization"),
        0,
        1,
        exclusive_minimum=True,
    )
    execution_seed = v.integer("$.execution.seed", execution.get("seed"), 0, 2147483647)
    v.exact("$.execution.trust_remote_code", execution.get("trust_remote_code"), False)

    attention = v.object(
        "$.execution.attention",
        execution.get("attention"),
        {"backend", "flash_attn_version"},
    )
    v.one_of(
        "$.execution.attention.backend",
        attention.get("backend"),
        ATTENTION_BACKENDS,
    )
    v.integer(
        "$.execution.attention.flash_attn_version",
        attention.get("flash_attn_version"),
        2,
        4,
    )

    compilation = v.object(
        "$.execution.compilation",
        execution.get("compilation"),
        {"cudagraph_mode", "mode"},
    )
    v.one_of(
        "$.execution.compilation.cudagraph_mode",
        compilation.get("cudagraph_mode"),
        {"NONE", "FULL", "PIECEWISE", "FULL_AND_PIECEWISE"},
    )
    v.integer("$.execution.compilation.mode", compilation.get("mode"), 0, 3)

    network = v.object(
        "$.execution.network", execution.get("network"), {"host", "port"}
    )
    v.exact("$.execution.network.host", network.get("host"), "127.0.0.1")
    v.integer("$.execution.network.port", network.get("port"), 1024, 65535)

    speculative_raw = root.get("speculative_decoding")
    speculative = v.object(
        "$.speculative_decoding",
        speculative_raw,
        {"enabled", "method", "num_speculative_tokens"},
        {
            "enabled",
            "method",
            "num_speculative_tokens",
            "prompt_lookup_min",
            "prompt_lookup_max",
        },
    )
    v.exact("$.speculative_decoding.enabled", speculative.get("enabled"), True)
    method = speculative.get("method")
    v.one_of("$.speculative_decoding.method", method, {"ngram", "suffix"})
    v.integer(
        "$.speculative_decoding.num_speculative_tokens",
        speculative.get("num_speculative_tokens"),
        1,
        16,
    )
    if method == "ngram":
        for required_key in ("prompt_lookup_min", "prompt_lookup_max"):
            if required_key not in speculative:
                v.error(
                    "$.speculative_decoding",
                    f"missing required field {required_key!r} for ngram",
                )
        lookup_min = v.integer(
            "$.speculative_decoding.prompt_lookup_min",
            speculative.get("prompt_lookup_min"),
            1,
            32,
        )
        lookup_max = v.integer(
            "$.speculative_decoding.prompt_lookup_max",
            speculative.get("prompt_lookup_max"),
            1,
            32,
        )
        if lookup_min is not None and lookup_max is not None and lookup_min > lookup_max:
            v.error(
                "$.speculative_decoding",
                "prompt_lookup_min must not exceed prompt_lookup_max",
            )
    elif method == "suffix":
        for forbidden in ("prompt_lookup_min", "prompt_lookup_max"):
            if forbidden in speculative:
                v.error(
                    "$.speculative_decoding",
                    f"field {forbidden!r} is forbidden for suffix",
                )

    sampling = v.object("$.sampling", root.get("sampling"), SAMPLING_KEYS)
    for key, expected in {
        "temperature": 0,
        "top_p": 1,
        "top_k": -1,
        "n": 1,
        "presence_penalty": 0,
        "frequency_penalty": 0,
    }.items():
        v.exact(f"$.sampling.{key}", sampling.get(key), expected)
    sampling_seed = v.integer("$.sampling.seed", sampling.get("seed"), 0, 2147483647)
    if (
        execution_seed is not None
        and sampling_seed is not None
        and execution_seed != sampling_seed
    ):
        v.error("$.sampling.seed", "must equal $.execution.seed")

    canary = v.object("$.canary", root.get("canary"), CANARY_KEYS)
    prompt = v.string(
        "$.canary.prompt",
        canary.get("prompt"),
        max_length=16384,
        reject_floating=False,
    )
    prompt_digest = v.sha256("$.canary.prompt_sha256", canary.get("prompt_sha256"))
    if prompt is not None and prompt_digest is not None:
        actual_prompt_digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if actual_prompt_digest != prompt_digest:
            v.error(
                "$.canary.prompt_sha256",
                f"does not match prompt bytes (expected {actual_prompt_digest})",
            )
    canary_max_tokens = v.integer(
        "$.canary.max_tokens", canary.get("max_tokens"), 1, 512
    )
    if (
        canary_max_tokens is not None
        and max_model_len is not None
        and canary_max_tokens > max_model_len
    ):
        v.error("$.canary.max_tokens", "must not exceed $.execution.max_model_len")
    v.sha256(
        "$.canary.expected_completion_sha256",
        canary.get("expected_completion_sha256"),
    )
    v.integer("$.canary.warmup_requests", canary.get("warmup_requests"), 1, 10000)
    v.integer(
        "$.canary.measured_requests", canary.get("measured_requests"), 1, 1000000
    )
    v.number(
        "$.canary.minimum_acceptance_rate",
        canary.get("minimum_acceptance_rate"),
        0,
        1,
    )
    v.exact(
        "$.canary.minimum_output_match_rate",
        canary.get("minimum_output_match_rate"),
        1,
    )

    return v.errors


def validate_lock(lock: Any) -> dict[str, Any]:
    """Return a validated lock or raise ``LockValidationError``."""

    errors = collect_validation_errors(lock)
    if errors:
        raise LockValidationError(errors)
    assert type(lock) is dict
    return lock


def load_and_validate_lock(path: str | Path) -> dict[str, Any]:
    return validate_lock(load_lock(path))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lock", type=Path, help="runtime-lock JSON to validate")
    parser.add_argument("--quiet", action="store_true", help="print only failures")
    args = parser.parse_args(argv)

    try:
        load_and_validate_lock(args.lock)
    except LockValidationError as exc:
        for error in exc.errors:
            print(f"invalid runtime lock: {error}", file=sys.stderr)
        return 2
    if not args.quiet:
        print(f"valid runtime lock: {args.lock}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
