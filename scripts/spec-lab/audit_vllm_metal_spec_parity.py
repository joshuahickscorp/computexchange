#!/usr/bin/env python3
"""Fail-closed output-parity audit for vLLM-Metal speculative decoding.

The Metal n-gram path verifies draft tokens with target-model logits, but that
does not by itself prove that a packed speculative forward is numerically
identical to ordinary greedy decode.  Near an argmax tie, a packed row can pick
a different token from a scalar row even when every draft was accepted.

This tool compares opaque benchmark captures that retain ``generated_texts``.
It deliberately does *not* calculate or publish a speedup.  It only answers
whether the baseline and speculative arms emitted exactly the same completion
bytes under a matched workload, and it requires a stable repeat for each arm
before returning success.

It accepts the raw local benchmark shape used by the M3 Ultra Metal experiment:

    python3 scripts/spec-lab/audit_vllm_metal_spec_parity.py \
      --baseline .artifacts/vllm-lab/m3-ultra-vllm-metal-sonnet-baseline-c1-repeat.json \
      --candidate .artifacts/vllm-lab/m3-ultra-vllm-metal-sonnet-ngram-k3-c1-repeat.json \
      --baseline-repeat .artifacts/vllm-lab/m3-ultra-vllm-metal-sonnet-baseline-c1-repeat2.json \
      --candidate-repeat .artifacts/vllm-lab/m3-ultra-vllm-metal-sonnet-ngram-k3-c1.json \
      --tokenizer-path /path/to/pinned/tokenizer \
      --output /tmp/cx-vllm-metal-parity.json

The optional tokenizer path is used only to identify the first mismatching token
ID.  Completion text is never copied into the receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


MAX_CAPTURE_BYTES = 64 << 20
SCHEMA_VERSION = 1
IDENTITY_FIELDS = (
    "host",
    "runtime",
    "core",
    "backend",
    "endpoint_type",
    "model_id",
    "tokenizer_id",
    "workload",
    "num_prompts",
    "total_input_tokens",
    "total_output_tokens",
    "input_lens",
    "output_lens",
)


class ParityAuditError(ValueError):
    """A capture is incomplete or cannot support a parity conclusion."""


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _duplicate_safe_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ParityAuditError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _load_json(path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ParityAuditError(f"cannot read {path}: {exc}") from exc
    if not raw:
        raise ParityAuditError(f"{path} is empty")
    if len(raw) > MAX_CAPTURE_BYTES:
        raise ParityAuditError(f"{path} exceeds {MAX_CAPTURE_BYTES} bytes")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_duplicate_safe_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ParityAuditError(f"non-finite JSON value {token!r}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParityAuditError(f"{path} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ParityAuditError(f"{path} root must be an object")
    return value, sha256_bytes(raw)


def _require_string(value: Mapping[str, Any], field: str, path: Path) -> str:
    candidate = value.get(field)
    if not isinstance(candidate, str) or not candidate:
        raise ParityAuditError(f"{path}: {field} must be a non-empty string")
    return candidate


def _require_nonnegative_int(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ParityAuditError(f"{label} must be a non-negative integer")
    return value


def _require_positive_int_list(value: Any, label: str, expected: int) -> tuple[int, ...]:
    if not isinstance(value, list) or len(value) != expected:
        raise ParityAuditError(f"{label} must be an array of {expected} positive integers")
    parsed: list[int] = []
    for index, item in enumerate(value):
        if type(item) is not int or item < 1:
            raise ParityAuditError(f"{label}[{index}] must be a positive integer")
        parsed.append(item)
    return tuple(parsed)


@dataclass(frozen=True, slots=True)
class BenchmarkCapture:
    """Validated, privacy-preserving view of a raw local benchmark capture."""

    source: str
    source_sha256: str
    identity: Mapping[str, Any]
    generated_texts: tuple[str, ...]
    output_array_sha256: str

    @property
    def prompt_count(self) -> int:
        return len(self.generated_texts)

    @classmethod
    def load(cls, path_value: str | Path) -> "BenchmarkCapture":
        path = Path(path_value)
        value, source_sha256 = _load_json(path)
        identity: dict[str, Any] = {}
        for field in IDENTITY_FIELDS:
            if field in {"num_prompts", "total_input_tokens", "total_output_tokens"}:
                identity[field] = _require_nonnegative_int(value.get(field), f"{path}: {field}")
            elif field in {"input_lens", "output_lens"}:
                # Validated below once num_prompts is known.
                continue
            else:
                identity[field] = _require_string(value, field, path)

        prompt_count = identity["num_prompts"]
        if prompt_count < 1:
            raise ParityAuditError(f"{path}: num_prompts must be >= 1")
        for field in ("completed", "failed"):
            _require_nonnegative_int(value.get(field), f"{path}: {field}")
        if value["completed"] != prompt_count or value["failed"] != 0:
            raise ParityAuditError(
                f"{path}: capture must have completed=num_prompts and failed=0"
            )
        errors = value.get("errors")
        if not isinstance(errors, list) or len(errors) != prompt_count or any(errors):
            raise ParityAuditError(f"{path}: errors must be an all-empty array per prompt")

        generated = value.get("generated_texts")
        if not isinstance(generated, list) or len(generated) != prompt_count:
            raise ParityAuditError(
                f"{path}: generated_texts must be an array of num_prompts strings"
            )
        if not all(isinstance(text, str) for text in generated):
            raise ParityAuditError(f"{path}: generated_texts must contain only strings")

        input_lens = _require_positive_int_list(
            value.get("input_lens"), f"{path}: input_lens", prompt_count
        )
        output_lens = _require_positive_int_list(
            value.get("output_lens"), f"{path}: output_lens", prompt_count
        )
        if sum(input_lens) != identity["total_input_tokens"]:
            raise ParityAuditError(f"{path}: total_input_tokens disagrees with input_lens")
        if sum(output_lens) != identity["total_output_tokens"]:
            raise ParityAuditError(f"{path}: total_output_tokens disagrees with output_lens")
        identity["input_lens"] = input_lens
        identity["output_lens"] = output_lens

        return cls(
            source=path.name,
            source_sha256=source_sha256,
            identity=identity,
            generated_texts=tuple(generated),
            output_array_sha256=sha256_json(generated),
        )


def _first_byte_difference(left: str, right: str) -> dict[str, int] | None:
    left_bytes = left.encode("utf-8")
    right_bytes = right.encode("utf-8")
    for index, (left_byte, right_byte) in enumerate(zip(left_bytes, right_bytes)):
        if left_byte != right_byte:
            return {
                "utf8_byte_offset": index,
                "baseline_byte": left_byte,
                "candidate_byte": right_byte,
            }
    if len(left_bytes) == len(right_bytes):
        return None
    index = min(len(left_bytes), len(right_bytes))
    return {
        "utf8_byte_offset": index,
        "baseline_byte": left_bytes[index] if index < len(left_bytes) else -1,
        "candidate_byte": right_bytes[index] if index < len(right_bytes) else -1,
    }


def first_token_difference(
    baseline_token_ids: Sequence[int], candidate_token_ids: Sequence[int]
) -> dict[str, int] | None:
    """Return the first token difference without retaining decoded text."""

    for index, (baseline_token, candidate_token) in enumerate(
        zip(baseline_token_ids, candidate_token_ids)
    ):
        if baseline_token != candidate_token:
            return {
                "token_index": index,
                "baseline_token_id": int(baseline_token),
                "candidate_token_id": int(candidate_token),
            }
    if len(baseline_token_ids) == len(candidate_token_ids):
        return None
    index = min(len(baseline_token_ids), len(candidate_token_ids))
    return {
        "token_index": index,
        "baseline_token_id": int(baseline_token_ids[index]) if index < len(baseline_token_ids) else -1,
        "candidate_token_id": int(candidate_token_ids[index]) if index < len(candidate_token_ids) else -1,
    }


def _load_tokenizer_encoder(path: Path) -> tuple[Callable[[str], list[int]], dict[str, Any]]:
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise ParityAuditError(
            "--tokenizer-path requires transformers; invoke this script with the "
            "pinned vLLM-Metal environment"
        ) from exc
    try:
        tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True)
    except Exception as exc:  # pragma: no cover - backend-specific error detail.
        raise ParityAuditError(f"cannot load local tokenizer at {path}: {exc}") from exc

    tokenizer_json = path / "tokenizer.json"
    tokenizer_sha256 = None
    if tokenizer_json.is_file():
        try:
            tokenizer_sha256 = sha256_bytes(tokenizer_json.read_bytes())
        except OSError as exc:
            raise ParityAuditError(f"cannot read {tokenizer_json}: {exc}") from exc

    def encode(text: str) -> list[int]:
        values = tokenizer.encode(text, add_special_tokens=False)
        if not isinstance(values, list) or not all(type(token) is int for token in values):
            raise ParityAuditError("tokenizer did not return a list of integer token IDs")
        return values

    return encode, {
        "path_sha256": sha256_bytes(str(path.resolve()).encode("utf-8")),
        "tokenizer_json_sha256": tokenizer_sha256,
    }


def _identity_mismatches(
    baseline: BenchmarkCapture, candidate: BenchmarkCapture
) -> list[str]:
    return [
        field
        for field in IDENTITY_FIELDS
        if baseline.identity.get(field) != candidate.identity.get(field)
    ]


def _stable_repeat(primary: BenchmarkCapture, repeat: BenchmarkCapture | None) -> bool | None:
    if repeat is None:
        return None
    return (
        not _identity_mismatches(primary, repeat)
        and primary.generated_texts == repeat.generated_texts
    )


def audit_captures(
    baseline: BenchmarkCapture,
    candidate: BenchmarkCapture,
    *,
    baseline_repeat: BenchmarkCapture | None = None,
    candidate_repeat: BenchmarkCapture | None = None,
    token_encoder: Callable[[str], list[int]] | None = None,
    tokenizer_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare one baseline/speculative pair and fail closed on any ambiguity."""

    identity_mismatches = _identity_mismatches(baseline, candidate)
    baseline_repeat_stable = _stable_repeat(baseline, baseline_repeat)
    candidate_repeat_stable = _stable_repeat(candidate, candidate_repeat)
    mismatches: list[dict[str, Any]] = []
    for index, (baseline_text, candidate_text) in enumerate(
        zip(baseline.generated_texts, candidate.generated_texts, strict=True)
    ):
        if baseline_text == candidate_text:
            continue
        mismatch: dict[str, Any] = {
            "prompt_index_zero_based": index,
            "prompt_index_one_based": index + 1,
            "baseline_text_sha256": sha256_bytes(baseline_text.encode("utf-8")),
            "candidate_text_sha256": sha256_bytes(candidate_text.encode("utf-8")),
            "baseline_utf8_bytes": len(baseline_text.encode("utf-8")),
            "candidate_utf8_bytes": len(candidate_text.encode("utf-8")),
            "first_byte_difference": _first_byte_difference(baseline_text, candidate_text),
        }
        if token_encoder is not None:
            baseline_ids = token_encoder(baseline_text)
            candidate_ids = token_encoder(candidate_text)
            mismatch["first_token_difference"] = first_token_difference(
                baseline_ids, candidate_ids
            )
            mismatch["baseline_token_count"] = len(baseline_ids)
            mismatch["candidate_token_count"] = len(candidate_ids)
        mismatches.append(mismatch)

    reasons: list[str] = []
    if identity_mismatches:
        reasons.append("baseline_candidate_identity_mismatch")
    if baseline_repeat is None or candidate_repeat is None:
        reasons.append("missing_stability_repeat")
    if baseline_repeat_stable is False:
        reasons.append("baseline_repeat_not_stable")
    if candidate_repeat_stable is False:
        reasons.append("candidate_repeat_not_stable")
    if mismatches:
        reasons.append("baseline_candidate_output_mismatch")

    if mismatches or identity_mismatches or baseline_repeat_stable is False or candidate_repeat_stable is False:
        status = "parity_failed"
    elif baseline_repeat is None or candidate_repeat is None:
        status = "inconclusive_missing_repeats"
    else:
        status = "parity_observed"

    return {
        "schema_version": SCHEMA_VERSION,
        "record_kind": "cx_vllm_metal_spec_parity_audit",
        "status": status,
        "claim_scope": (
            "Exact-output diagnostic only. This receipt never promotes a throughput, "
            "capacity, price, or production claim."
        ),
        "baseline": {
            "source": baseline.source,
            "source_sha256": baseline.source_sha256,
            "identity_sha256": sha256_json(baseline.identity),
            "output_array_sha256": baseline.output_array_sha256,
            "prompt_count": baseline.prompt_count,
        },
        "candidate": {
            "source": candidate.source,
            "source_sha256": candidate.source_sha256,
            "identity_sha256": sha256_json(candidate.identity),
            "output_array_sha256": candidate.output_array_sha256,
            "prompt_count": candidate.prompt_count,
        },
        "repeatability": {
            "baseline_repeat_provided": baseline_repeat is not None,
            "candidate_repeat_provided": candidate_repeat is not None,
            "baseline_repeat_stable": baseline_repeat_stable,
            "candidate_repeat_stable": candidate_repeat_stable,
        },
        "comparability": {
            "identity_fields": list(IDENTITY_FIELDS),
            "identity_mismatch_fields": identity_mismatches,
            "matched_workload": not identity_mismatches,
        },
        "correctness": {
            "paired_prompts": baseline.prompt_count,
            "exact_match_count": baseline.prompt_count - len(mismatches),
            "mismatch_count": len(mismatches),
            "first_mismatch": mismatches[0] if mismatches else None,
            "tokenizer_identity": dict(tokenizer_identity) if tokenizer_identity else None,
        },
        "guard": {
            "candidate_lossless_for_this_matrix": status == "parity_observed",
            "speed_claim_valid": False,
            "production_enablement_valid": False,
            "reason_codes": reasons,
        },
    }


def write_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(
            json.dumps(receipt, sort_keys=True, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise ParityAuditError(f"cannot write {path}: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--baseline-repeat", type=Path)
    parser.add_argument("--candidate-repeat", type=Path)
    parser.add_argument(
        "--tokenizer-path",
        type=Path,
        help="optional pinned local tokenizer directory for token-ID diagnostics",
    )
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        token_encoder = None
        tokenizer_identity = None
        if args.tokenizer_path is not None:
            token_encoder, tokenizer_identity = _load_tokenizer_encoder(args.tokenizer_path)
        receipt = audit_captures(
            BenchmarkCapture.load(args.baseline),
            BenchmarkCapture.load(args.candidate),
            baseline_repeat=(
                BenchmarkCapture.load(args.baseline_repeat)
                if args.baseline_repeat is not None
                else None
            ),
            candidate_repeat=(
                BenchmarkCapture.load(args.candidate_repeat)
                if args.candidate_repeat is not None
                else None
            ),
            token_encoder=token_encoder,
            tokenizer_identity=tokenizer_identity,
        )
        if args.output is not None:
            write_receipt(args.output, receipt)
        print(json.dumps(receipt, sort_keys=True, indent=2))
        return 0 if receipt["status"] == "parity_observed" else 2
    except ParityAuditError as exc:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "record_kind": "cx_vllm_metal_spec_parity_audit_error",
                    "status": "error",
                    "error": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
