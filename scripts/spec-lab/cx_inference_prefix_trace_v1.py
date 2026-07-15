#!/usr/bin/env python3
"""Fail-closed CX telemetry binding for native shared-prefix cache traces.

This module validates the namespaced ``cx_prefix_cache_trace`` extension on
an otherwise OpenAI-compatible completion response.  It is deliberately a
telemetry boundary, not a cache implementation or a performance scorer.

The bridge must provide a canonical immutable trace artifact separately from
the completion response.  The response trace binds that artifact to one
customer-visible request through a fresh nonce, the complete request digest,
the declared shared-prefix digest, the runtime digest, and the delivered token
IDs.  A bare ``prefix_cache_hit`` boolean is intentionally insufficient.

Only two outcomes are accepted:

* ``native_prefix_hit``: a verified ``vllm_native_prefix_kv`` hit with no
  response-cache or speculative-decode use; or
* ``fallback_direct_decode``: an explicit *non-hit* with a direct-target
  fallback.  It is safe to deliver but must not be counted as prefix reuse.

The contract is local telemetry binding, not an independent attestation of a
remote server.  Callers must retain and separately attest the bridge/runtime
when a stronger claim is required.
"""

from __future__ import annotations

from dataclasses import dataclass
import argparse
import copy
import hashlib
import json
import math
import re
import sys
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
TRACE_KIND = "cx_inference_prefix_cache_trace_v1"
TRACE_ARTIFACT_KIND = "cx_inference_prefix_cache_trace_artifact_v1"
TRACE_FIELD = "cx_prefix_cache_trace"
NATIVE_PREFIX_BACKEND = "vllm_native_prefix_kv"
OUTCOME_NATIVE_PREFIX_HIT = "native_prefix_hit"
OUTCOME_FALLBACK_DIRECT_DECODE = "fallback_direct_decode"
OUTCOMES = frozenset({OUTCOME_NATIVE_PREFIX_HIT, OUTCOME_FALLBACK_DIRECT_DECODE})
MAX_JSON_BYTES = 8 << 20
MAX_TOKENS = 32_768
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_TRACE_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "bridge_id",
        "bridge_config_sha256",
        "trace_nonce",
        "request_digest_sha256",
        "prefix_token_ids_sha256",
        "prefix_token_count",
        "runtime_sha256",
        "endpoint_attestation_sha256",
        "tenant_scope_sha256",
        "prefix_prime_receipt_sha256",
        "cache_instance_sha256",
        "cache_generation_sha256",
        "engine_input_token_ids_sha256",
        "native_prefix_block_size",
        "native_cached_token_count",
        "completion_token_ids_sha256",
        "trace_artifact_sha256",
        "cache_backend",
        "prefix_cache_hit",
        "response_cache_hit",
        "speculative_decode_used",
        "direct_target_fallback_available",
        "direct_target_fallback_used",
        "outcome",
        "trace_sha256",
    }
)
_ARTIFACT_FIELDS = frozenset(_TRACE_FIELDS - {"trace_artifact_sha256", "trace_sha256"})


class PrefixTraceError(ValueError):
    """The bridge response cannot safely prove the declared cache outcome."""


@dataclass(frozen=True)
class TraceExpectation:
    """Values the caller binds before issuing a single completion request."""

    bridge_id: str
    bridge_config_sha256: str
    trace_nonce: str
    request_digest_sha256: str
    prefix_token_ids_sha256: str
    prefix_token_count: int
    runtime_sha256: str
    endpoint_attestation_sha256: str
    tenant_scope_sha256: str
    prefix_prime_receipt_sha256: str
    cache_instance_sha256: str
    cache_generation_sha256: str
    engine_input_token_ids_sha256: str
    native_prefix_block_size: int

    def validate(self) -> None:
        _identifier(self.bridge_id, "expectation.bridge_id")
        _sha(self.bridge_config_sha256, "expectation.bridge_config_sha256")
        _nonce(self.trace_nonce, "expectation.trace_nonce")
        _sha(self.request_digest_sha256, "expectation.request_digest_sha256")
        _sha(self.prefix_token_ids_sha256, "expectation.prefix_token_ids_sha256")
        _integer(self.prefix_token_count, "expectation.prefix_token_count", minimum=1)
        _sha(self.runtime_sha256, "expectation.runtime_sha256")
        _sha(self.endpoint_attestation_sha256, "expectation.endpoint_attestation_sha256")
        _sha(self.tenant_scope_sha256, "expectation.tenant_scope_sha256")
        _sha(self.prefix_prime_receipt_sha256, "expectation.prefix_prime_receipt_sha256")
        _sha(self.cache_instance_sha256, "expectation.cache_instance_sha256")
        _sha(self.cache_generation_sha256, "expectation.cache_generation_sha256")
        _sha(self.engine_input_token_ids_sha256, "expectation.engine_input_token_ids_sha256")
        _integer(self.native_prefix_block_size, "expectation.native_prefix_block_size", minimum=1)


@dataclass(frozen=True)
class ValidatedPrefixTrace:
    """Validated completion evidence; ``is_native_prefix_hit`` is the gate."""

    outcome: str
    is_native_prefix_hit: bool
    completion_token_ids: tuple[int, ...]
    completion_token_ids_sha256: str
    trace_sha256: str
    trace_artifact_sha256: str
    trace: Mapping[str, Any]
    artifact: Mapping[str, Any]


def canonical_json_bytes(value: Any) -> bytes:
    """Canonical JSON bytes used for all contract digests."""

    try:
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise PrefixTraceError("canonical_json_failed") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def trace_sha256(value: Mapping[str, Any]) -> str:
    """Return the self-hash for a trace, excluding its self-reference."""

    unsigned = copy.deepcopy(dict(value))
    unsigned.pop("trace_sha256", None)
    return sha256_json(unsigned)


def derive_trace_nonce(
    *,
    request_digest_sha256: str,
    prefix_token_ids_sha256: str,
    runtime_sha256: str,
    trial_identity: str,
) -> str:
    """Create a deterministic, per-call nonce from a unique trial identity.

    ``trial_identity`` should contain a newly allocated trial/result path or a
    cryptographically random request nonce.  It is deliberately not inferred
    from a reusable cache key.
    """

    _sha(request_digest_sha256, "nonce.request_digest_sha256")
    _sha(prefix_token_ids_sha256, "nonce.prefix_token_ids_sha256")
    _sha(runtime_sha256, "nonce.runtime_sha256")
    _nonce(trial_identity, "nonce.trial_identity")
    return sha256_json(
        {
            "contract": "cx-inference-prefix-trace-nonce-v1",
            "request_digest_sha256": request_digest_sha256,
            "prefix_token_ids_sha256": prefix_token_ids_sha256,
            "runtime_sha256": runtime_sha256,
            "trial_identity": trial_identity,
        }
    )


def _duplicate_safe_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PrefixTraceError(f"duplicate_json_key_{key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise PrefixTraceError(f"nonfinite_json_constant_{value}")


def _parse_json_bytes(raw: bytes, label: str) -> dict[str, Any]:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_JSON_BYTES:
        raise PrefixTraceError(f"{label}_size_invalid")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_duplicate_safe_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, PrefixTraceError) as exc:
        raise PrefixTraceError(f"{label}_invalid_json") from exc
    if not isinstance(value, dict):
        raise PrefixTraceError(f"{label}_root_must_be_object")
    if canonical_json_bytes(value) != raw:
        raise PrefixTraceError(f"{label}_must_use_canonical_json")
    return value


def _exact(value: Any, expected: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise PrefixTraceError(f"{label}_must_be_object")
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing or unknown:
        raise PrefixTraceError(
            f"{label}_fields_invalid_missing_{','.join(missing) or 'none'}"
            f"_unknown_{','.join(unknown) or 'none'}"
        )
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise PrefixTraceError(f"{label}_must_be_lowercase_sha256")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,127}", value):
        raise PrefixTraceError(f"{label}_must_be_lowercase_identifier")
    return value


def _nonce(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
        raise PrefixTraceError(f"{label}_must_be_nonempty_string")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0, maximum: int = MAX_TOKENS) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise PrefixTraceError(f"{label}_must_be_integer_in_range")
    return value


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise PrefixTraceError(f"{label}_must_be_boolean")
    return value


def _tokens(value: Any, label: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_TOKENS:
        raise PrefixTraceError(f"{label}_count_invalid")
    return tuple(
        _integer(token, f"{label}_{index}", maximum=(1 << 32) - 1)
        for index, token in enumerate(value)
    )


def _stable(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return json.loads(canonical_json_bytes(value).decode("utf-8"))


def _completion_tokens(response: Mapping[str, Any]) -> tuple[int, ...]:
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise PrefixTraceError("openai_response_choices_invalid")
    choice = choices[0]
    if choice.get("index") != 0:
        raise PrefixTraceError("openai_response_choice_index_invalid")
    fields = [field for field in ("token_ids", "cx_completion_token_ids") if field in choice]
    if len(fields) != 1:
        raise PrefixTraceError("openai_response_requires_exactly_one_completion_token_field")
    return _tokens(choice[fields[0]], "openai_response_completion_token_ids")


def _parse_artifact(value: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = _exact(value, _ARTIFACT_FIELDS, "trace_artifact")
    if raw["schema_version"] != SCHEMA_VERSION or raw["kind"] != TRACE_ARTIFACT_KIND:
        raise PrefixTraceError("trace_artifact_identity_invalid")
    _identifier(raw["bridge_id"], "trace_artifact.bridge_id")
    for field in (
        "bridge_config_sha256",
        "request_digest_sha256",
        "prefix_token_ids_sha256",
        "runtime_sha256",
        "endpoint_attestation_sha256",
        "tenant_scope_sha256",
        "prefix_prime_receipt_sha256",
        "cache_instance_sha256",
        "cache_generation_sha256",
        "engine_input_token_ids_sha256",
        "completion_token_ids_sha256",
    ):
        _sha(raw[field], f"trace_artifact.{field}")
    _nonce(raw["trace_nonce"], "trace_artifact.trace_nonce")
    _integer(raw["prefix_token_count"], "trace_artifact.prefix_token_count", minimum=1)
    _integer(raw["native_prefix_block_size"], "trace_artifact.native_prefix_block_size", minimum=1)
    _integer(raw["native_cached_token_count"], "trace_artifact.native_cached_token_count")
    if raw["cache_backend"] != NATIVE_PREFIX_BACKEND:
        raise PrefixTraceError("trace_artifact_requires_native_prefix_backend")
    for field in (
        "prefix_cache_hit",
        "response_cache_hit",
        "speculative_decode_used",
        "direct_target_fallback_available",
        "direct_target_fallback_used",
    ):
        _boolean(raw[field], f"trace_artifact.{field}")
    if raw["outcome"] not in OUTCOMES:
        raise PrefixTraceError("trace_artifact_outcome_invalid")
    return _stable(raw)


def _parse_trace(value: Any) -> Mapping[str, Any]:
    raw = _exact(value, _TRACE_FIELDS, "prefix_trace")
    if raw["schema_version"] != SCHEMA_VERSION or raw["kind"] != TRACE_KIND:
        raise PrefixTraceError("prefix_trace_identity_invalid")
    _identifier(raw["bridge_id"], "prefix_trace.bridge_id")
    for field in (
        "bridge_config_sha256",
        "request_digest_sha256",
        "prefix_token_ids_sha256",
        "runtime_sha256",
        "endpoint_attestation_sha256",
        "tenant_scope_sha256",
        "prefix_prime_receipt_sha256",
        "cache_instance_sha256",
        "cache_generation_sha256",
        "engine_input_token_ids_sha256",
        "completion_token_ids_sha256",
        "trace_artifact_sha256",
        "trace_sha256",
    ):
        _sha(raw[field], f"prefix_trace.{field}")
    _nonce(raw["trace_nonce"], "prefix_trace.trace_nonce")
    _integer(raw["prefix_token_count"], "prefix_trace.prefix_token_count", minimum=1)
    _integer(raw["native_prefix_block_size"], "prefix_trace.native_prefix_block_size", minimum=1)
    _integer(raw["native_cached_token_count"], "prefix_trace.native_cached_token_count")
    if raw["cache_backend"] != NATIVE_PREFIX_BACKEND:
        raise PrefixTraceError("prefix_trace_requires_native_prefix_backend")
    for field in (
        "prefix_cache_hit",
        "response_cache_hit",
        "speculative_decode_used",
        "direct_target_fallback_available",
        "direct_target_fallback_used",
    ):
        _boolean(raw[field], f"prefix_trace.{field}")
    if raw["outcome"] not in OUTCOMES:
        raise PrefixTraceError("prefix_trace_outcome_invalid")
    stable = _stable(raw)
    if trace_sha256(stable) != stable["trace_sha256"]:
        raise PrefixTraceError("prefix_trace_self_hash_mismatch")
    return stable


def _artifact_bytes(value: Mapping[str, Any] | bytes) -> tuple[Mapping[str, Any], bytes]:
    if isinstance(value, bytes):
        raw = value
        parsed = _parse_json_bytes(raw, "trace_artifact")
    elif isinstance(value, Mapping):
        parsed = dict(value)
        raw = canonical_json_bytes(parsed)
    else:
        raise PrefixTraceError("trace_artifact_must_be_mapping_or_bytes")
    return _parse_artifact(parsed), raw


def _validate_semantic_outcome(trace: Mapping[str, Any]) -> bool:
    """Return whether the trace is a qualifying native-prefix hit."""

    if trace["response_cache_hit"]:
        raise PrefixTraceError("response_cache_use_forbidden")
    if trace["speculative_decode_used"]:
        raise PrefixTraceError("speculative_decode_use_forbidden")
    if not trace["direct_target_fallback_available"]:
        raise PrefixTraceError("direct_target_fallback_must_be_available")
    if trace["prefix_cache_hit"]:
        if trace["outcome"] != OUTCOME_NATIVE_PREFIX_HIT:
            raise PrefixTraceError("native_prefix_hit_outcome_mismatch")
        if trace["direct_target_fallback_used"]:
            raise PrefixTraceError("native_prefix_hit_cannot_use_fallback")
        if trace["native_cached_token_count"] < 1:
            raise PrefixTraceError("native_prefix_hit_requires_cached_tokens")
        if trace["native_cached_token_count"] > trace["prefix_token_count"]:
            raise PrefixTraceError("native_prefix_cached_token_count_exceeds_prefix")
        return True
    if trace["outcome"] != OUTCOME_FALLBACK_DIRECT_DECODE:
        raise PrefixTraceError("prefix_nonhit_must_be_explicit_direct_fallback")
    if not trace["direct_target_fallback_used"]:
        raise PrefixTraceError("prefix_nonhit_requires_direct_fallback")
    if trace["native_cached_token_count"] != 0:
        raise PrefixTraceError("prefix_nonhit_must_not_report_cached_tokens")
    return False


def validate_openai_response(
    response: Mapping[str, Any],
    *,
    expectation: TraceExpectation,
    trace_artifact: Mapping[str, Any] | bytes,
) -> ValidatedPrefixTrace:
    """Validate one OpenAI-compatible response and its bridge trace artifact.

    The return value does not blur a fallback into a hit: callers must check
    ``is_native_prefix_hit`` before recording reuse coverage or a multiplier.
    """

    if not isinstance(response, Mapping):
        raise PrefixTraceError("openai_response_must_be_mapping")
    expectation.validate()
    if TRACE_FIELD not in response:
        raise PrefixTraceError("openai_response_missing_cx_prefix_cache_trace")
    tokens = _completion_tokens(response)
    trace = _parse_trace(response[TRACE_FIELD])
    artifact, artifact_raw = _artifact_bytes(trace_artifact)
    artifact_digest = sha256_bytes(artifact_raw)
    if trace["trace_artifact_sha256"] != artifact_digest:
        raise PrefixTraceError("prefix_trace_artifact_digest_mismatch")
    # The artifact is a durable bridge record, so every semantically relevant
    # trace field must agree exactly.  No caller-supplied hit flag can bypass
    # this binding.
    for field in _ARTIFACT_FIELDS - {"schema_version", "kind"}:
        if trace[field] != artifact[field]:
            raise PrefixTraceError(f"prefix_trace_artifact_field_mismatch_{field}")
    expected = {
        "bridge_id": expectation.bridge_id,
        "bridge_config_sha256": expectation.bridge_config_sha256,
        "trace_nonce": expectation.trace_nonce,
        "request_digest_sha256": expectation.request_digest_sha256,
        "prefix_token_ids_sha256": expectation.prefix_token_ids_sha256,
        "prefix_token_count": expectation.prefix_token_count,
        "runtime_sha256": expectation.runtime_sha256,
        "endpoint_attestation_sha256": expectation.endpoint_attestation_sha256,
        "tenant_scope_sha256": expectation.tenant_scope_sha256,
        "prefix_prime_receipt_sha256": expectation.prefix_prime_receipt_sha256,
        "cache_instance_sha256": expectation.cache_instance_sha256,
        "cache_generation_sha256": expectation.cache_generation_sha256,
        "engine_input_token_ids_sha256": expectation.engine_input_token_ids_sha256,
        "native_prefix_block_size": expectation.native_prefix_block_size,
        "completion_token_ids_sha256": sha256_json(list(tokens)),
    }
    for field, value in expected.items():
        if trace[field] != value:
            raise PrefixTraceError(f"prefix_trace_expectation_mismatch_{field}")
    hit = _validate_semantic_outcome(trace)
    return ValidatedPrefixTrace(
        outcome=str(trace["outcome"]),
        is_native_prefix_hit=hit,
        completion_token_ids=tokens,
        completion_token_ids_sha256=str(trace["completion_token_ids_sha256"]),
        trace_sha256=str(trace["trace_sha256"]),
        trace_artifact_sha256=artifact_digest,
        trace=trace,
        artifact=artifact,
    )


def status() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "cx_inference_prefix_trace_status_v1",
        "measurement_status": "unmeasured",
        "response_field": TRACE_FIELD,
        "accepted_outcomes": [OUTCOME_NATIVE_PREFIX_HIT, OUTCOME_FALLBACK_DIRECT_DECODE],
        "required_native_backend": NATIVE_PREFIX_BACKEND,
        "forbidden": ["response_cache_hit=true", "speculative_decode_used=true"],
        "required_bindings": [
            "endpoint_attestation_sha256",
            "tenant_scope_sha256",
            "prefix_prime_receipt_sha256",
            "cache_instance_sha256",
            "cache_generation_sha256",
            "engine_input_token_ids_sha256",
            "native_prefix_block_size",
            "native_cached_token_count",
        ],
        "claim": "telemetry validation only; no speed, score, authorization, or production claim",
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="emit the unmeasured trace contract")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.status:
        print("cx prefix trace contract rejected: --status is required", file=sys.stderr)
        return 2
    print(json.dumps(status(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
