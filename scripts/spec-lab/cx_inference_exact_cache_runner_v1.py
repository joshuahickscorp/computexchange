#!/usr/bin/env python3
"""Physical exact-response-cache arm runner for the CX inference ABBA gate.

This file is deliberately a narrow *measurement adapter*.  It turns an
already-running OpenAI-compatible target endpoint plus a retained, direct
target-produced response cache into the concrete command expected by
``screen_inference_lane_abba.py``.  It does not start an engine, simulate
latency, create a score, or authorize a product route.

The protocol has three commands:

``prime``
    Send each predeclared eligible request to the real target endpoint and
    write an immutable exact-response-cache artifact.  Priming represents a
    prior customer request, so it is intentionally outside an ABBA trial.

``emit-arm-manifests``
    Generate pinned baseline/candidate manifests that invoke this runner.  The
    baseline always calls the target; the candidate returns only an exact,
    tenant/model/runtime/token/payload-bound cache entry and directly calls the
    target on every cache miss.

``run``
    Execute one arm inside an ABBA trial and write the arm result plus every
    charged stage receipt.  The surrounding harness owns the end-to-end clock.

The target must return token IDs in either ``choices[0].token_ids`` (the vLLM
``return_token_ids: true`` completion extension) or
``choices[0].cx_completion_token_ids`` (a CX adapter).  Text alone is
intentionally insufficient: exact-token parity is the minimum evidence needed
by the strict receipt adapter.  A cache hit is keyed by the CX-owned complete
request identity *and* the canonical OpenAI payload digest; it cannot cross
tenant, model, tokenizer, runtime, sampling, input-token, or payload
boundaries.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time
from typing import Any, Mapping, Sequence
import urllib.error
import urllib.parse
import urllib.request


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import cx_inference_policy_v1 as policy  # noqa: E402
import cx_vllm_endpoint_attestation_v1 as endpoint_attestation  # noqa: E402
import screen_inference_lane_abba as abba  # noqa: E402


SCHEMA_VERSION = 1
WORKLOAD_KIND = "cx_inference_exact_cache_workload_v1"
CACHE_KIND = "cx_inference_exact_response_cache_v1"
MAX_INPUT_BYTES = 8 << 20
MAX_HTTP_RESPONSE_BYTES = 8 << 20
MAX_REQUESTS = 4_096
MAX_OUTPUT_TOKENS = 32_768
MAX_TIMEOUT_SECONDS = 7_200
ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

STAGES = abba.STAGES
AUTHORIZATION_FIELDS = (
    "artifact_verified",
    "customer_selectable",
    "publication_eligible",
    "production_ready",
    "billing_eligible",
)

# The existing ABBA harness stores one required non-null candidate config
# digest for exact reuse.  This is an execution-config identity for this
# cache adapter, not a speculative proposer and never a speed multiplier.
EXACT_REQUEST_KEY_SCHEMA = {
    "contract": "cx-exact-response-cache-key-v1",
    "request_identity": "cx_inference_policy_v1.request_identity_sha256",
    "completion_payload": "canonical-json-sha256-v1",
    "completion_tokens": "canonical-json-sha256-v1",
}
EXACT_REQUEST_KEY_SCHEMA_SHA256 = ""  # Set after canonical helpers are defined.
EXACT_CACHE_EXECUTION_CONFIG = {
    "contract": "cx-exact-response-cache-runner-v1",
    "lookup": "request-identity-plus-canonical-payload",
    "miss": "target-only-direct-decode",
    "token_parity": "exact-output-token-ids",
}
EXACT_CACHE_EXECUTION_CONFIG_SHA256 = ""  # Set after canonical helpers are defined.
DIRECT_TARGET_FALLBACK_POLICY = {
    "mode": "target_only_direct_decode",
    "trigger": "parity_or_confidence_failure",
}
DIRECT_TARGET_FALLBACK_POLICY_SHA256 = ""  # Set after canonical helpers are defined.


class ExactCacheRunnerError(ValueError):
    """Input, cache, target, or manifest evidence cannot safely be used."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep the endpoint in the pinned workload instead of silently hopping."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ExactCacheRunnerError("canonical_json_failed") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


EXACT_REQUEST_KEY_SCHEMA_SHA256 = sha256_json(EXACT_REQUEST_KEY_SCHEMA)
EXACT_CACHE_EXECUTION_CONFIG_SHA256 = sha256_json(EXACT_CACHE_EXECUTION_CONFIG)
DIRECT_TARGET_FALLBACK_POLICY_SHA256 = sha256_json(DIRECT_TARGET_FALLBACK_POLICY)


def _duplicate_safe_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExactCacheRunnerError(f"duplicate_json_key_{key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ExactCacheRunnerError(f"nonfinite_json_constant_{value}")


def _read_json_file(path_value: str | Path, label: str, *, maximum: int = MAX_INPUT_BYTES) -> dict[str, Any]:
    path = Path(path_value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ExactCacheRunnerError(f"{label}_path_unsafe")
    try:
        before = path.stat()
        raw = path.read_bytes()
        after = path.stat()
    except OSError as exc:
        raise ExactCacheRunnerError(f"{label}_unreadable") from exc
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    # Reading can update atime on some local filesystems.  It is not content
    # mutation, so intentionally exclude it while retaining inode/size/mtime/
    # ctime checks just like the surrounding ABBA harness.
    if not stat.S_ISREG(before.st_mode) or before_identity != after_identity or not raw or len(raw) > maximum:
        raise ExactCacheRunnerError(f"{label}_changed_or_invalid_size")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_duplicate_safe_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ExactCacheRunnerError) as exc:
        raise ExactCacheRunnerError(f"{label}_invalid_json") from exc
    if not isinstance(value, dict):
        raise ExactCacheRunnerError(f"{label}_root_must_be_object")
    return value


def _exact(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExactCacheRunnerError(f"{label}_must_be_object")
    missing = sorted(fields - set(value))
    unknown = sorted(set(value) - fields)
    if missing or unknown:
        raise ExactCacheRunnerError(
            f"{label}_fields_invalid_missing_{','.join(missing) or 'none'}"
            f"_unknown_{','.join(unknown) or 'none'}"
        )
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ExactCacheRunnerError(f"{label}_must_be_lowercase_sha256")
    return value


def _string(value: Any, label: str, *, maximum: int = 4_096) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise ExactCacheRunnerError(f"{label}_must_be_nonempty_string")
    return value


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ExactCacheRunnerError(f"{label}_must_be_boolean")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ExactCacheRunnerError(f"{label}_must_be_integer_in_range")
    return value


def _finite_number(value: Any, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ExactCacheRunnerError(f"{label}_must_be_finite_number")
    return float(value)


def _stable(value: Any) -> Any:
    """Return a JSON-only stable deep copy or fail before it can be hashed."""

    return json.loads(canonical_json_bytes(value).decode("utf-8"))


def _tokens(value: Any, label: str, *, minimum: int = 0, maximum: int = MAX_OUTPUT_TOKENS) -> list[int]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ExactCacheRunnerError(f"{label}_count_invalid")
    return [_integer(token, f"{label}_{index}", maximum=(1 << 32) - 1) for index, token in enumerate(value)]


def _require_new_absolute_path(path_value: str | Path, label: str) -> Path:
    path = Path(path_value)
    if not path.is_absolute() or path.exists() or path.is_symlink() or path.parent.is_symlink() or not path.parent.is_dir():
        raise ExactCacheRunnerError(f"{label}_must_be_new_absolute_path")
    return path


def _write_new(path: Path, payload: bytes, label: str) -> None:
    path = _require_new_absolute_path(path, label)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ExactCacheRunnerError(f"{label}_write_failed") from exc
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ExactCacheRunnerError(f"{label}_short_write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _parse_endpoint(value: Any) -> dict[str, Any]:
    raw = _exact(value, {"url", "timeout_secs", "authorization_env"}, "workload.endpoint")
    url = _string(raw["url"], "workload.endpoint.url", maximum=4_096)
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ExactCacheRunnerError("workload_endpoint_url_invalid")
    timeout = _finite_number(raw["timeout_secs"], "workload.endpoint.timeout_secs")
    if not 0.001 <= timeout <= MAX_TIMEOUT_SECONDS:
        raise ExactCacheRunnerError("workload_endpoint_timeout_out_of_range")
    authorization_env = raw["authorization_env"]
    if authorization_env is not None:
        authorization_env = _string(authorization_env, "workload.endpoint.authorization_env", maximum=128)
        if not ENV_RE.fullmatch(authorization_env):
            raise ExactCacheRunnerError("workload_endpoint_authorization_env_invalid")
    return {"url": url, "timeout_secs": timeout, "authorization_env": authorization_env}


def _request_digest(value: Mapping[str, Any]) -> str:
    """Digest the exact request envelope that must stay the same across arms."""

    return sha256_json(
        {
            "request": value["request"],
            "input_token_ids": value["input_token_ids"],
            "completion_request": value["completion_request"],
        }
    )


def _validate_completion_body(
    value: Any,
    *,
    request: Mapping[str, Any],
    logical: Mapping[str, Any],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExactCacheRunnerError(f"{label}_must_be_object")
    required = {"model", "prompt", "max_tokens", "temperature", "top_p", "seed", "n"}
    if not required.issubset(value):
        raise ExactCacheRunnerError(f"{label}_missing_required_openai_fields")
    if value["model"] != request["model_id"]:
        raise ExactCacheRunnerError(f"{label}_model_mismatch")
    if value["max_tokens"] != logical["max_output_tokens"]:
        raise ExactCacheRunnerError(f"{label}_max_tokens_mismatch")
    if value["temperature"] != 0 or value["top_p"] != 1:
        raise ExactCacheRunnerError(f"{label}_sampling_not_greedy")
    if value["seed"] != logical["sampling"]["seed"] or value["n"] != 1:
        raise ExactCacheRunnerError(f"{label}_sampling_contract_mismatch")
    if value.get("stream", False) is not False:
        raise ExactCacheRunnerError(f"{label}_streaming_forbidden")
    # The body is exactly hashed, including supported engine-specific fields.
    # Retain a JSON-only canonical copy before it is passed to urllib.
    return _stable(value)


def _validate_workload_request(
    value: Any,
    *,
    index: int,
    logical: Mapping[str, Any],
    runtime: Mapping[str, Any] | None,
    eligible_indexes: set[int],
) -> dict[str, Any]:
    raw = _exact(
        value,
        {"request_index", "request", "input_token_ids", "completion_request"},
        f"workload.requests[{index}]",
    )
    if _integer(raw["request_index"], f"workload.requests[{index}].request_index") != index:
        raise ExactCacheRunnerError("workload_request_indexes_must_be_contiguous")
    try:
        request = policy._validate_request(raw["request"])
    except policy.PolicyInputError as exc:
        raise ExactCacheRunnerError(f"workload_request_identity_invalid_{index}") from exc
    input_tokens = _tokens(
        raw["input_token_ids"],
        f"workload.requests[{index}].input_token_ids",
        maximum=1_000_000,
    )
    if request["input_token_ids_sha256"] != sha256_json(input_tokens):
        raise ExactCacheRunnerError(f"workload_request_input_token_digest_mismatch_{index}")
    if request["model_id"] != logical["model"]["model_id"] or request["model_revision"] != logical["model"]["model_revision"]:
        raise ExactCacheRunnerError(f"workload_request_model_binding_mismatch_{index}")
    if request["sampling_contract_sha256"] != logical["sampling_contract_sha256"]:
        raise ExactCacheRunnerError(f"workload_request_sampling_binding_mismatch_{index}")
    if request["max_output_tokens"] != logical["max_output_tokens"] or request["concurrency"] != logical["concurrency"]:
        raise ExactCacheRunnerError(f"workload_request_execution_binding_mismatch_{index}")
    if request["shared_prefix_token_ids_sha256"] is not None or request["shared_prefix_token_count"] != 0:
        raise ExactCacheRunnerError(f"workload_request_prefix_reuse_forbidden_{index}")
    if request["prefix_reuse_authorized"]:
        raise ExactCacheRunnerError(f"workload_request_prefix_authorization_forbidden_{index}")
    if bool(request["response_reuse_authorized"]) != (index in eligible_indexes):
        raise ExactCacheRunnerError(f"workload_request_reuse_authorization_mismatch_{index}")
    if runtime is not None:
        if request["runtime_sha256"] != runtime["runtime_sha256"]:
            raise ExactCacheRunnerError(f"workload_request_runtime_binding_mismatch_{index}")
        if request["tokenizer_sha256"] != runtime["tokenizer_sha256"]:
            raise ExactCacheRunnerError(f"workload_request_tokenizer_binding_mismatch_{index}")
    completion_request = _validate_completion_body(
        raw["completion_request"],
        request=request,
        logical=logical,
        label=f"workload.requests[{index}].completion_request",
    )
    return {
        "request_index": index,
        "request": _stable(request),
        "input_token_ids": input_tokens,
        "completion_request": completion_request,
        "request_digest": "",  # filled after the stable envelope is complete
        "completion_request_sha256": sha256_json(completion_request),
    }


def load_workload(path_value: str | Path, *, runtime: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Load and fully bind one exact-request-reuse workload contract."""

    raw = _read_json_file(path_value, "workload")
    value = _exact(raw, {"schema_version", "kind", "logical_work", "endpoint", "requests"}, "workload")
    if value["schema_version"] != SCHEMA_VERSION or value["kind"] != WORKLOAD_KIND:
        raise ExactCacheRunnerError("workload_identity_invalid")
    try:
        logical = abba._parse_logical_work(value["logical_work"], "exact_request_reuse")
    except abba.InferenceLaneContractError as exc:
        raise ExactCacheRunnerError("workload_logical_work_invalid") from exc
    if logical.value["reuse_contract"]["exact_request_key_schema_sha256"] != EXACT_REQUEST_KEY_SCHEMA_SHA256:
        raise ExactCacheRunnerError("workload_exact_request_key_schema_unrecognized")
    endpoint = _parse_endpoint(value["endpoint"])
    raw_requests = value["requests"]
    if not isinstance(raw_requests, list) or not 1 <= len(raw_requests) <= MAX_REQUESTS:
        raise ExactCacheRunnerError("workload_requests_invalid")
    if len(raw_requests) != len(logical.request_digests):
        raise ExactCacheRunnerError("workload_request_count_mismatch")
    eligible_indexes = set(logical.eligible_indexes)
    requests = [
        _validate_workload_request(
            row,
            index=index,
            logical=logical.value,
            runtime=runtime,
            eligible_indexes=eligible_indexes,
        )
        for index, row in enumerate(raw_requests)
    ]
    for request in requests:
        request["request_digest"] = _request_digest(request)
    request_digests = [request["request_digest"] for request in requests]
    if tuple(request_digests) != logical.request_digests:
        raise ExactCacheRunnerError("workload_request_digests_do_not_bind_requests")
    if logical.value["corpus_sha256"] != sha256_json(
        [
            {
                "request": request["request"],
                "input_token_ids": request["input_token_ids"],
                "completion_request": request["completion_request"],
            }
            for request in requests
        ]
    ):
        raise ExactCacheRunnerError("workload_corpus_digest_does_not_bind_requests")
    if logical.value["input_token_ids_sha256"] != sha256_json(
        [request["input_token_ids"] for request in requests]
    ):
        raise ExactCacheRunnerError("workload_input_token_digest_does_not_bind_requests")
    stable = {
        "schema_version": SCHEMA_VERSION,
        "kind": WORKLOAD_KIND,
        "logical_work": _stable(logical.value),
        "endpoint": endpoint,
        "requests": [
            {
                "request_index": request["request_index"],
                "request": request["request"],
                "input_token_ids": request["input_token_ids"],
                "completion_request": request["completion_request"],
            }
            for request in requests
        ],
    }
    return {
        "value": stable,
        "sha256": sha256_json(stable),
        "logical": logical,
        "endpoint": endpoint,
        "requests": requests,
    }


def _validate_endpoint_attestation(
    path_value: Path | str,
    *,
    workload: Mapping[str, Any],
    runtime: Mapping[str, Any],
) -> dict[str, Any]:
    """Reject an HTTP route unless a frozen server identity binds it first."""

    try:
        return endpoint_attestation.validate_for_workload(
            path_value,
            endpoint_url=str(workload["endpoint"]["url"]),
            model_id=str(workload["logical"].value["model"]["model_id"]),
            model_revision=str(workload["logical"].value["model"]["model_revision"]),
            runtime=runtime,
        )
    except endpoint_attestation.EndpointAttestationError as exc:
        raise ExactCacheRunnerError("endpoint_attestation_invalid") from exc


def _http_completion(endpoint: Mapping[str, Any], body: Mapping[str, Any]) -> list[int]:
    """Issue one real target call and require an exact token-ID response."""

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "computeexchange-exact-cache-abba/1",
    }
    authorization_env = endpoint["authorization_env"]
    if authorization_env is not None:
        value = os.environ.get(str(authorization_env))
        if not value:
            raise ExactCacheRunnerError("target_authorization_env_missing")
        headers["Authorization"] = f"Bearer {value}"
    request = urllib.request.Request(
        str(endpoint["url"]),
        data=canonical_json_bytes(body),
        headers=headers,
        method="POST",
    )
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=float(endpoint["timeout_secs"])) as response:
            raw = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
            status = getattr(response, "status", 200)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ExactCacheRunnerError("target_request_failed") from exc
    if status != 200 or not raw or len(raw) > MAX_HTTP_RESPONSE_BYTES:
        raise ExactCacheRunnerError("target_response_status_or_size_invalid")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_duplicate_safe_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ExactCacheRunnerError) as exc:
        raise ExactCacheRunnerError("target_response_invalid_json") from exc
    if not isinstance(payload, dict):
        raise ExactCacheRunnerError("target_response_root_invalid")
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ExactCacheRunnerError("target_response_choices_invalid")
    choice = choices[0]
    if choice.get("index") != 0:
        raise ExactCacheRunnerError("target_response_choice_index_invalid")
    # vLLM emits ``token_ids`` when the canonical request contains
    # ``return_token_ids: true``.  A CX-owned endpoint may instead expose the
    # namespaced field.  Do not derive IDs from text or logprobs strings.
    token_field = (
        "token_ids"
        if "token_ids" in choice
        else "cx_completion_token_ids"
        if "cx_completion_token_ids" in choice
        else None
    )
    if token_field is None:
        raise ExactCacheRunnerError("target_response_missing_exact_completion_token_ids")
    return _tokens(
        choice[token_field],
        "target_response_completion_token_ids",
        minimum=1,
    )


def _cache_semantic(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": value["schema_version"],
        "kind": value["kind"],
        "workload_sha256": value["workload_sha256"],
        "logical_work_sha256": value["logical_work_sha256"],
        "entries": value["entries"],
    }


def _validate_cache_entry(value: Any, *, index: int, logical: abba.LogicalWork) -> dict[str, Any]:
    raw = _exact(
        value,
        {
            "request_index",
            "request",
            "completion_request_sha256",
            "completion_token_ids",
            "completion_token_ids_sha256",
            "target_output_token_ids_sha256",
            "source_direct_target_validated",
        },
        f"cache.entries[{index}]",
    )
    request_index = _integer(raw["request_index"], f"cache.entries[{index}].request_index")
    if request_index not in logical.eligible_indexes:
        raise ExactCacheRunnerError("cache_entry_not_predeclared_eligible")
    try:
        request = policy._validate_request(raw["request"])
    except policy.PolicyInputError as exc:
        raise ExactCacheRunnerError("cache_entry_request_identity_invalid") from exc
    body_sha = _sha(raw["completion_request_sha256"], "cache_entry_completion_request_sha256")
    tokens = _tokens(raw["completion_token_ids"], "cache_entry_completion_token_ids", minimum=1)
    if len(tokens) > int(logical.value["max_output_tokens"]):
        raise ExactCacheRunnerError("cache_entry_exceeds_pinned_output_cap")
    token_sha = _sha(raw["completion_token_ids_sha256"], "cache_entry_completion_token_ids_sha256")
    target_sha = _sha(raw["target_output_token_ids_sha256"], "cache_entry_target_output_token_ids_sha256")
    if token_sha != sha256_json(tokens) or target_sha != token_sha:
        raise ExactCacheRunnerError("cache_entry_token_parity_binding_invalid")
    if not _boolean(raw["source_direct_target_validated"], "cache_entry_source_direct_target_validated"):
        raise ExactCacheRunnerError("cache_entry_direct_target_not_validated")
    return {
        "request_index": request_index,
        "request": _stable(request),
        "completion_request_sha256": body_sha,
        "completion_token_ids": tokens,
        "completion_token_ids_sha256": token_sha,
        "target_output_token_ids_sha256": target_sha,
        "source_direct_target_validated": True,
    }


def load_cache(path_value: str | Path, *, workload: Mapping[str, Any]) -> dict[str, Any]:
    raw = _read_json_file(path_value, "cache")
    value = _exact(
        raw,
        {
            "schema_version",
            "kind",
            "workload_sha256",
            "logical_work_sha256",
            "entries",
            "cache_history_sha256",
        },
        "cache",
    )
    if value["schema_version"] != SCHEMA_VERSION or value["kind"] != CACHE_KIND:
        raise ExactCacheRunnerError("cache_identity_invalid")
    if _sha(value["workload_sha256"], "cache_workload_sha256") != workload["sha256"]:
        raise ExactCacheRunnerError("cache_workload_binding_mismatch")
    logical: abba.LogicalWork = workload["logical"]
    if _sha(value["logical_work_sha256"], "cache_logical_work_sha256") != logical.sha256:
        raise ExactCacheRunnerError("cache_logical_work_binding_mismatch")
    raw_entries = value["entries"]
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ExactCacheRunnerError("cache_entries_invalid")
    entries = [_validate_cache_entry(row, index=index, logical=logical) for index, row in enumerate(raw_entries)]
    entry_indexes = [entry["request_index"] for entry in entries]
    if entry_indexes != sorted(entry_indexes) or len(set(entry_indexes)) != len(entry_indexes):
        raise ExactCacheRunnerError("cache_entries_must_be_sorted_unique")
    if set(entry_indexes) != set(logical.eligible_indexes):
        raise ExactCacheRunnerError("cache_entries_do_not_cover_predeclared_eligible_requests")
    semantic = {
        "schema_version": SCHEMA_VERSION,
        "kind": CACHE_KIND,
        "workload_sha256": value["workload_sha256"],
        "logical_work_sha256": value["logical_work_sha256"],
        "entries": entries,
    }
    history = _sha(value["cache_history_sha256"], "cache_history_sha256")
    if history != sha256_json(semantic):
        raise ExactCacheRunnerError("cache_history_digest_mismatch")
    return {
        "value": {**semantic, "cache_history_sha256": history},
        "history_sha256": history,
        "entries": {entry["request_index"]: entry for entry in entries},
    }


def prime_cache(
    *,
    workload_path: Path,
    runtime_path: Path,
    endpoint_attestation_path: Path,
    cache_out: Path,
) -> dict[str, Any]:
    """Make retained direct-target cache entries for every declared eligible request."""

    runtime = _load_runtime(runtime_path, "prime_runtime")
    workload = load_workload(workload_path, runtime=runtime)
    attestation = _validate_endpoint_attestation(
        endpoint_attestation_path, workload=workload, runtime=runtime
    )
    entries: list[dict[str, Any]] = []
    for request in workload["requests"]:
        if request["request_index"] not in workload["logical"].eligible_indexes:
            continue
        tokens = _http_completion(workload["endpoint"], request["completion_request"])
        if len(tokens) > int(workload["logical"].value["max_output_tokens"]):
            raise ExactCacheRunnerError("cache_prime_completion_exceeds_pinned_output_cap")
        token_sha = sha256_json(tokens)
        entries.append(
            {
                "request_index": request["request_index"],
                "request": request["request"],
                "completion_request_sha256": request["completion_request_sha256"],
                "completion_token_ids": tokens,
                "completion_token_ids_sha256": token_sha,
                "target_output_token_ids_sha256": token_sha,
                "source_direct_target_validated": True,
            }
        )
    if not entries:
        raise ExactCacheRunnerError("cache_prime_has_no_eligible_requests")
    semantic = {
        "schema_version": SCHEMA_VERSION,
        "kind": CACHE_KIND,
        "workload_sha256": workload["sha256"],
        "logical_work_sha256": workload["logical"].sha256,
        "entries": entries,
    }
    cache = {**semantic, "cache_history_sha256": sha256_json(semantic)}
    _write_new(cache_out, canonical_json_bytes(cache) + b"\n", "cache_out")
    return {
        "cache_path": str(cache_out),
        "cache_history_sha256": cache["cache_history_sha256"],
        "workload_sha256": workload["sha256"],
        "logical_work_sha256": workload["logical"].sha256,
        "entry_count": len(entries),
        "endpoint_attestation_sha256": attestation["attestation_sha256"],
        "claim": "cache priming only; no performance measurement or score",
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                digest.update(chunk)
    except OSError as exc:
        raise ExactCacheRunnerError("pinned_file_hash_failed") from exc
    return digest.hexdigest()


def _load_runtime(path: Path, label: str) -> dict[str, Any]:
    raw = _read_json_file(path, label)
    try:
        return _stable(abba._parse_runtime(raw))
    except abba.InferenceLaneContractError as exc:
        raise ExactCacheRunnerError(f"{label}_invalid") from exc


def _load_authorization(path: Path | None) -> dict[str, bool]:
    if path is None:
        return {field: False for field in AUTHORIZATION_FIELDS}
    raw = _read_json_file(path, "authorization")
    value = _exact(raw, set(AUTHORIZATION_FIELDS), "authorization")
    return {field: _boolean(value[field], f"authorization.{field}") for field in AUTHORIZATION_FIELDS}


def _baseline_cache_history(workload_sha256: str) -> str:
    return sha256_json(
        {
            "contract": "cx-target-only-no-reuse-cache-history-v1",
            "workload_sha256": workload_sha256,
        }
    )


def _output_templates() -> tuple[str, dict[str, str]]:
    result = "{trial_dir}/arm-result.json"
    stages = {stage: f"{{trial_dir}}/{stage}.json" for stage in STAGES}
    return result, stages


def _manifest_command(
    *,
    runner: Path,
    manifest_path: Path,
    workload_path: Path,
    endpoint_attestation_path: Path,
    cache_path: Path | None,
    arm: str,
) -> tuple[list[str], dict[str, str]]:
    result, stages = _output_templates()
    argv = [
        str(runner),
        "run",
        "--arm",
        arm,
        "--arm-manifest",
        str(manifest_path),
        "--workload",
        str(workload_path),
        "--endpoint-attestation",
        str(endpoint_attestation_path),
    ]
    if cache_path is not None:
        argv.extend(["--cache-artifact", str(cache_path)])
    argv.extend(["--result-out", result])
    for stage in STAGES:
        argv.extend([f"--stage-{stage.replace('_', '-')}-out", stages[stage]])
    return argv, stages


def _manifest_pins(
    runner: Path,
    workload_path: Path,
    endpoint_attestation_path: Path,
    cache_path: Path | None,
) -> list[dict[str, str]]:
    paths = [
        ("command_executable", runner),
        ("workload_contract", workload_path),
        ("endpoint_attestation", endpoint_attestation_path),
    ]
    if cache_path is not None:
        paths.append(("exact_response_cache", cache_path))
    rows: list[dict[str, str]] = []
    for role, path in paths:
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise ExactCacheRunnerError(f"manifest_pin_{role}_unsafe")
        rows.append({"role": role, "path": str(path), "sha256": _sha256_file(path)})
    return rows


def _assert_runtime_pair(baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> None:
    shared_fields = (
        "backend",
        "host_hardware_sha256",
        "core_sha256",
        "runtime_sha256",
        "engine_id",
        "engine_commit",
        "metal_runtime_sha256",
        "weights_sha256",
        "tokenizer_sha256",
        "precision_id",
    )
    if any(baseline[field] != candidate[field] for field in shared_fields):
        raise ExactCacheRunnerError("baseline_candidate_runtime_not_same_engine")


def emit_arm_manifests(
    *,
    workload_path: Path,
    cache_path: Path,
    endpoint_attestation_path: Path,
    baseline_runtime_path: Path,
    candidate_runtime_path: Path,
    baseline_out: Path,
    candidate_out: Path,
    authorization_path: Path | None,
    timeout_secs: int,
    runner_path: Path | None = None,
) -> dict[str, Any]:
    """Write a ready-to-run, pinned exact-cache baseline/candidate manifest pair."""

    if not 1 <= timeout_secs <= MAX_TIMEOUT_SECONDS:
        raise ExactCacheRunnerError("manifest_timeout_out_of_range")
    runner = (runner_path or Path(__file__)).resolve()
    if not runner.is_file() or runner.is_symlink() or not os.access(runner, os.X_OK):
        raise ExactCacheRunnerError("runner_path_must_be_executable_regular_file")
    workload = load_workload(workload_path)
    cache = load_cache(cache_path, workload=workload)
    baseline_runtime = _load_runtime(baseline_runtime_path, "baseline_runtime")
    candidate_runtime = _load_runtime(candidate_runtime_path, "candidate_runtime")
    _assert_runtime_pair(baseline_runtime, candidate_runtime)
    # Each individual request identity is bound to this exact runtime/tokenizer.
    load_workload(workload_path, runtime=baseline_runtime)
    attestation = _validate_endpoint_attestation(
        endpoint_attestation_path, workload=workload, runtime=baseline_runtime
    )
    _validate_endpoint_attestation(
        endpoint_attestation_path, workload=workload, runtime=candidate_runtime
    )
    authorization = _load_authorization(authorization_path)
    baseline_out = _require_new_absolute_path(baseline_out, "baseline_manifest_out")
    candidate_out = _require_new_absolute_path(candidate_out, "candidate_manifest_out")
    if baseline_out == candidate_out:
        raise ExactCacheRunnerError("manifest_outputs_must_differ")
    result_template, stage_templates = _output_templates()
    baseline_argv, _ = _manifest_command(
        runner=runner,
        manifest_path=baseline_out,
        workload_path=workload_path.resolve(),
        endpoint_attestation_path=endpoint_attestation_path.resolve(),
        cache_path=None,
        arm="baseline",
    )
    candidate_argv, _ = _manifest_command(
        runner=runner,
        manifest_path=candidate_out,
        workload_path=workload_path.resolve(),
        endpoint_attestation_path=endpoint_attestation_path.resolve(),
        cache_path=cache_path.resolve(),
        arm="candidate",
    )
    common = {
        "schema_version": abba.SCHEMA_VERSION,
        "kind": abba.ARM_MANIFEST_KIND,
        "lane": "exact_request_reuse",
        "logical_work": _stable(workload["logical"].value),
        "authorization": authorization,
        "trial_outputs": {"arm_result": result_template, "stages": stage_templates},
    }
    baseline = {
        **common,
        "arm": "baseline",
        "runtime": baseline_runtime,
        "cache_policy": {
            "mode": "target_only_no_reuse",
            "cache_history_sha256": _baseline_cache_history(workload["sha256"]),
            "response_cache_enabled": False,
            "prefix_cache_enabled": False,
            "fallback_policy_sha256": DIRECT_TARGET_FALLBACK_POLICY_SHA256,
            "proposer_config_sha256": None,
        },
        "command": {
            "argv": baseline_argv,
            "timeout_secs": timeout_secs,
            "pinned_files": _manifest_pins(
                runner,
                workload_path.resolve(),
                endpoint_attestation_path.resolve(),
                None,
            ),
        },
    }
    candidate = {
        **common,
        "arm": "candidate",
        "runtime": candidate_runtime,
        "cache_policy": {
            "mode": "exact_request_reuse",
            "cache_history_sha256": cache["history_sha256"],
            "response_cache_enabled": True,
            "prefix_cache_enabled": False,
            "fallback_policy_sha256": DIRECT_TARGET_FALLBACK_POLICY_SHA256,
            "proposer_config_sha256": EXACT_CACHE_EXECUTION_CONFIG_SHA256,
        },
        "command": {
            "argv": candidate_argv,
            "timeout_secs": timeout_secs,
            "pinned_files": _manifest_pins(
                runner,
                workload_path.resolve(),
                endpoint_attestation_path.resolve(),
                cache_path.resolve(),
            ),
        },
    }
    # Use the same parser that the outer harness will use.  This catches broken
    # templates/pins before either immutable output is created.
    for value, label in ((baseline, "baseline"), (candidate, "candidate")):
        try:
            # The parser consumes a path, so this is only structural preflight.
            abba._parse_logical_work(value["logical_work"], "exact_request_reuse")
            abba._parse_runtime(value["runtime"])
            abba._parse_cache_policy(value["cache_policy"], lane="exact_request_reuse", arm=label)
            abba._parse_authorization(value["authorization"])
        except abba.InferenceLaneContractError as exc:
            raise ExactCacheRunnerError(f"generated_{label}_manifest_invalid") from exc
    _write_new(baseline_out, canonical_json_bytes(baseline) + b"\n", "baseline_manifest_out")
    try:
        _write_new(candidate_out, canonical_json_bytes(candidate) + b"\n", "candidate_manifest_out")
    except Exception:
        # Do not delete an immutable artifact we did manage to create.  The
        # caller can inspect it; the failure is explicit and no benchmark ran.
        raise
    return {
        "baseline_manifest": str(baseline_out),
        "candidate_manifest": str(candidate_out),
        "cache_history_sha256": cache["history_sha256"],
        "workload_sha256": workload["sha256"],
        "endpoint_attestation_sha256": attestation["attestation_sha256"],
        "claim": "manifest preparation only; no performance measurement or score",
    }


def _verify_command_binding(
    manifest: abba.ArmManifest,
    *,
    result_out: Path,
    actual_argv: Sequence[str] | None = None,
) -> None:
    """Make direct or resident invocation obey the pinned arm argv.

    ``actual_argv`` is supplied only by the resident worker, which executes
    the same pinned arm contract inside a pre-started service instead of
    spawning a fresh interpreter for every customer request.  The worker must
    pass the complete expanded command vector; it cannot invent a shorter
    in-process variant.
    """

    if not result_out.is_absolute():
        raise ExactCacheRunnerError("result_out_must_be_absolute")
    trial_dir = result_out.parent.resolve(strict=False)
    expected = tuple(item.replace("{trial_dir}", str(trial_dir)) for item in manifest.command)
    actual = (
        (str(Path(sys.argv[0]).resolve()), *sys.argv[1:])
        if actual_argv is None
        else tuple(actual_argv)
    )
    if actual != expected:
        raise ExactCacheRunnerError("command_argv_does_not_match_pinned_arm_manifest")


def _stage_paths_from_args(args: argparse.Namespace) -> dict[str, Path]:
    paths = {stage: Path(getattr(args, f"stage_{stage}_out")) for stage in STAGES}
    result_parent = Path(args.result_out).parent.resolve(strict=False)
    if any(not path.is_absolute() or path.parent.resolve(strict=False) != result_parent for path in paths.values()):
        raise ExactCacheRunnerError("stage_outputs_must_share_absolute_trial_directory")
    if len(set(paths.values())) != len(paths) or Path(args.result_out) in set(paths.values()):
        raise ExactCacheRunnerError("trial_output_paths_must_be_distinct")
    return paths


def _write_stage_receipt(
    path: Path,
    *,
    manifest: abba.ArmManifest,
    stage: str,
    elapsed_ns: int,
    artifact_sha256: str,
) -> str:
    value = {
        "schema_version": abba.SCHEMA_VERSION,
        "kind": abba.STAGE_RECEIPT_KIND,
        "stage": stage,
        "arm": manifest.name,
        "lane": manifest.logical_work.lane,
        "logical_work_sha256": manifest.logical_work.sha256,
        "runtime_identity_sha256": manifest.runtime_sha256,
        "cache_policy_sha256": manifest.cache_policy_sha256,
        "completed": True,
        "included_in_end_to_end_wall": True,
        "elapsed_ns": elapsed_ns,
        "artifact_sha256": artifact_sha256,
    }
    payload = canonical_json_bytes(value) + b"\n"
    _write_new(path, payload, f"stage_{stage}_out")
    return sha256_bytes(payload)


def _output_row(request: Mapping[str, Any], tokens: Sequence[int]) -> dict[str, Any]:
    result = list(tokens)
    return {
        "request_index": request["request_index"],
        "request_sha256": request["request_digest"],
        "completion_token_ids": result,
        "completion_token_ids_sha256": sha256_json(result),
        "completion_token_count": len(result),
    }


def _cache_hit_for_request(
    cache: Mapping[str, Any], request: Mapping[str, Any]) -> list[int] | None:
    entry = cache["entries"].get(request["request_index"])
    if entry is None:
        return None
    if (
        entry["request"] != request["request"]
        or entry["completion_request_sha256"] != request["completion_request_sha256"]
        or entry["completion_token_ids_sha256"] != entry["target_output_token_ids_sha256"]
        or not entry["source_direct_target_validated"]
    ):
        return None
    return list(entry["completion_token_ids"])


def run_arm(
    args: argparse.Namespace,
    *,
    actual_argv: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Run exactly one actual target/cache arm and write ABBA-compatible receipts."""

    manifest_path = Path(args.arm_manifest)
    if not manifest_path.is_absolute():
        raise ExactCacheRunnerError("arm_manifest_must_be_absolute")
    try:
        manifest = abba.load_arm_manifest(manifest_path, args.arm)
    except abba.InferenceLaneContractError as exc:
        raise ExactCacheRunnerError("arm_manifest_invalid") from exc
    if manifest.logical_work.lane != "exact_request_reuse":
        raise ExactCacheRunnerError("arm_manifest_lane_must_be_exact_request_reuse")
    result_out = Path(args.result_out)
    stage_paths = _stage_paths_from_args(args)
    _verify_command_binding(manifest, result_out=result_out, actual_argv=actual_argv)
    destinations = [result_out, *(stage_paths[stage] for stage in STAGES)]
    if any(not path.is_absolute() or path.exists() or path.is_symlink() for path in destinations):
        raise ExactCacheRunnerError("trial_outputs_must_be_new_absolute_paths")

    started = time.perf_counter_ns()
    admission_started = started
    workload = load_workload(Path(args.workload), runtime=manifest.runtime)
    _validate_endpoint_attestation(
        Path(args.endpoint_attestation), workload=workload, runtime=manifest.runtime
    )
    if workload["logical"].sha256 != manifest.logical_work.sha256 or workload["logical"].value != manifest.logical_work.value:
        raise ExactCacheRunnerError("workload_does_not_match_pinned_arm_logical_work")
    if manifest.name == "candidate" and args.cache_artifact is None:
        raise ExactCacheRunnerError("candidate_requires_exact_response_cache")
    if manifest.name == "baseline" and args.cache_artifact is not None:
        raise ExactCacheRunnerError("baseline_forbids_exact_response_cache")
    cache: dict[str, Any] | None = None
    if manifest.name == "candidate":
        cache = load_cache(Path(args.cache_artifact), workload=workload)
        if cache["history_sha256"] != manifest.cache_policy["cache_history_sha256"]:
            raise ExactCacheRunnerError("candidate_cache_history_does_not_match_pinned_policy")
    admission_elapsed = time.perf_counter_ns() - admission_started

    routing_started = time.perf_counter_ns()
    eligible = set(manifest.logical_work.eligible_indexes)
    routes = [
        "exact_response_cache" if manifest.name == "candidate" and request["request_index"] in eligible else "target_only_direct_decode"
        for request in workload["requests"]
    ]
    routing_elapsed = time.perf_counter_ns() - routing_started

    lookup_started = time.perf_counter_ns()
    cached_tokens: dict[int, list[int]] = {}
    cache_miss_indexes: list[int] = []
    if cache is not None:
        for request in workload["requests"]:
            index = request["request_index"]
            if index not in eligible:
                continue
            hit = _cache_hit_for_request(cache, request)
            if hit is None:
                cache_miss_indexes.append(index)
            else:
                cached_tokens[index] = hit
    lookup_elapsed = time.perf_counter_ns() - lookup_started

    execution_started = time.perf_counter_ns()
    direct_requests = [
        request for request in workload["requests"] if request["request_index"] not in cached_tokens
    ]
    direct_request_indexes = [request["request_index"] for request in direct_requests]
    direct_tokens: dict[int, list[int]] = {}
    if direct_requests:
        # Honor the declared same-work concurrency cap in both arms.  Cache
        # hits need no target worker; direct baseline/fallback requests are
        # submitted in stable request order and collected by that same order.
        workers = min(int(manifest.logical_work.value["concurrency"]), len(direct_requests))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(_http_completion, workload["endpoint"], request["completion_request"])
                for request in direct_requests
            ]
            for request, future in zip(direct_requests, futures, strict=True):
                direct_tokens[request["request_index"]] = future.result()
    outputs: list[dict[str, Any]] = []
    reuse_outcomes: list[dict[str, Any]] = []
    for request in workload["requests"]:
        index = request["request_index"]
        hit = index in cached_tokens
        if hit:
            tokens = cached_tokens[index]
        else:
            tokens = direct_tokens[index]
        outputs.append(_output_row(request, tokens))
        reuse_outcomes.append({"request_index": index, "eligible": index in eligible, "hit": hit})
    execution_elapsed = time.perf_counter_ns() - execution_started

    verification_started = time.perf_counter_ns()
    if any(row["completion_token_count"] < 1 for row in outputs):
        raise ExactCacheRunnerError("empty_completion_cannot_qualify")
    if any(row["completion_token_count"] > manifest.logical_work.value["max_output_tokens"] for row in outputs):
        raise ExactCacheRunnerError("completion_exceeds_pinned_output_cap")
    if manifest.name == "baseline" and any(row["hit"] for row in reuse_outcomes):
        raise ExactCacheRunnerError("baseline_reuse_forbidden")
    if manifest.name == "candidate":
        for index in eligible:
            if index not in cached_tokens and index not in direct_request_indexes:
                raise ExactCacheRunnerError("candidate_cache_miss_not_directly_repaired")
    delivery_digest = sha256_json([row["completion_token_ids_sha256"] for row in outputs])
    verification_elapsed = time.perf_counter_ns() - verification_started

    serialization_started = time.perf_counter_ns()
    fallback_used = bool(cache_miss_indexes)
    core_result = {
        "schema_version": abba.SCHEMA_VERSION,
        "kind": abba.ARM_RESULT_KIND,
        "arm": manifest.name,
        "lane": manifest.logical_work.lane,
        "logical_work_sha256": manifest.logical_work.sha256,
        "runtime_identity_sha256": manifest.runtime_sha256,
        "cache_policy_sha256": manifest.cache_policy_sha256,
        "outputs": outputs,
        "reuse_outcomes": reuse_outcomes,
        "fallback": {
            "direct_target_decode_available": True,
            "direct_target_decode_validated": True,
            "used": fallback_used,
            "reason_code": "exact_cache_miss" if fallback_used else "none",
        },
        "delivery_output_sha256": delivery_digest,
    }
    serialization_artifact = sha256_json(core_result)
    serialization_elapsed = time.perf_counter_ns() - serialization_started

    delivery_started = time.perf_counter_ns()
    # Delivery is the exact stable serialized token payload that the arm result
    # later cross-links.  It is deliberately not a text re-tokenization.
    delivery_artifact = delivery_digest
    delivery_elapsed = time.perf_counter_ns() - delivery_started

    artifact_payloads = {
        "admission": {
            "workload_sha256": workload["sha256"],
            "logical_work_sha256": manifest.logical_work.sha256,
            "arm": manifest.name,
        },
        "routing": {"routes": routes, "eligible_request_indexes": sorted(eligible)},
        "cache_lookup": {
            "cache_history_sha256": cache["history_sha256"] if cache is not None else None,
            "hits": sorted(cached_tokens),
            "misses": cache_miss_indexes,
        },
        "engine_execution": {
            "direct_request_indexes": direct_request_indexes,
            "direct_output_token_ids_sha256": [
                row["completion_token_ids_sha256"]
                for row in outputs
                if row["request_index"] in direct_request_indexes
            ],
        },
        "verification": {
            "delivery_output_sha256": delivery_digest,
            "cache_hits": sorted(cached_tokens),
            "cache_miss_indexes": cache_miss_indexes,
        },
        "serialization": {"arm_result_core_sha256": serialization_artifact},
        "delivery": delivery_artifact,
    }
    elapsed_by_stage = {
        "admission": admission_elapsed,
        "routing": routing_elapsed,
        "cache_lookup": lookup_elapsed,
        "engine_execution": execution_elapsed,
        "verification": verification_elapsed,
        "serialization": serialization_elapsed,
        "delivery": delivery_elapsed,
    }
    stage_hashes: dict[str, str] = {}
    for stage in STAGES:
        artifact = (
            delivery_artifact if stage == "delivery" else sha256_json(artifact_payloads[stage])
        )
        stage_hashes[stage] = _write_stage_receipt(
            stage_paths[stage],
            manifest=manifest,
            stage=stage,
            elapsed_ns=elapsed_by_stage[stage],
            artifact_sha256=artifact,
        )
    result = {**core_result, "stage_receipts": stage_hashes}
    _write_new(result_out, canonical_json_bytes(result) + b"\n", "result_out")
    return {
        "arm": manifest.name,
        "direct_request_count": len(direct_request_indexes),
        "cache_hit_count": len(cached_tokens),
        "cache_miss_count": len(cache_miss_indexes),
        "run_elapsed_ns": time.perf_counter_ns() - started,
        "claim": "one physical arm only; the ABBA harness determines any comparison",
    }


def status() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "cx_inference_exact_cache_runner_status_v1",
        "measurement_status": "unmeasured",
        "lane": "exact_request_reuse",
        "workload_kind": WORKLOAD_KIND,
        "cache_kind": CACHE_KIND,
        "required_target_response_fields": [
            "choices[0].token_ids (vLLM: return_token_ids=true)",
            "choices[0].cx_completion_token_ids (CX adapter)",
        ],
        "exact_request_key_schema_sha256": EXACT_REQUEST_KEY_SCHEMA_SHA256,
        "exact_cache_execution_config_sha256": EXACT_CACHE_EXECUTION_CONFIG_SHA256,
        "direct_target_fallback_policy_sha256": DIRECT_TARGET_FALLBACK_POLICY_SHA256,
        "next_steps": [
            "create one pinned greedy workload with input token IDs",
            "attest the matching launched vLLM endpoint and freeze its startup log",
            "prime it through the real target endpoint",
            "emit pinned arm manifests",
            "run screen_inference_lane_abba.py with eight ABBA trial pairs and --qualifying-receipt-out",
        ],
        "claim": "no speed, score, product, billing, or authorization claim",
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="print the unmeasured exact-cache contract")

    prime = subparsers.add_parser("prime", help="make a direct-target exact-response cache artifact")
    prime.add_argument("--workload", type=Path, required=True)
    prime.add_argument("--runtime", type=Path, required=True)
    prime.add_argument("--endpoint-attestation", type=Path, required=True)
    prime.add_argument("--cache-out", type=Path, required=True)

    manifests = subparsers.add_parser(
        "emit-arm-manifests", help="make pinned baseline/candidate manifests for the ABBA harness"
    )
    manifests.add_argument("--workload", type=Path, required=True)
    manifests.add_argument("--cache-artifact", type=Path, required=True)
    manifests.add_argument("--endpoint-attestation", type=Path, required=True)
    manifests.add_argument("--baseline-runtime", type=Path, required=True)
    manifests.add_argument("--candidate-runtime", type=Path, required=True)
    manifests.add_argument("--baseline-manifest-out", type=Path, required=True)
    manifests.add_argument("--candidate-manifest-out", type=Path, required=True)
    manifests.add_argument("--authorization", type=Path)
    manifests.add_argument("--timeout-secs", type=int, default=MAX_TIMEOUT_SECONDS)
    manifests.add_argument("--runner-path", type=Path)

    run = subparsers.add_parser("run", help="execute one physical baseline or cache arm")
    run.add_argument("--arm", choices=("baseline", "candidate"), required=True)
    run.add_argument("--arm-manifest", type=Path, required=True)
    run.add_argument("--workload", type=Path, required=True)
    run.add_argument("--endpoint-attestation", type=Path, required=True)
    run.add_argument("--cache-artifact", type=Path)
    run.add_argument("--result-out", type=Path, required=True)
    for stage in STAGES:
        run.add_argument(f"--stage-{stage.replace('_', '-')}-out", dest=f"stage_{stage}_out", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "status":
            result = status()
        elif args.command == "prime":
            result = prime_cache(
                workload_path=args.workload,
                runtime_path=args.runtime,
                endpoint_attestation_path=args.endpoint_attestation,
                cache_out=args.cache_out,
            )
        elif args.command == "emit-arm-manifests":
            result = emit_arm_manifests(
                workload_path=args.workload,
                cache_path=args.cache_artifact,
                endpoint_attestation_path=args.endpoint_attestation,
                baseline_runtime_path=args.baseline_runtime,
                candidate_runtime_path=args.candidate_runtime,
                baseline_out=args.baseline_manifest_out,
                candidate_out=args.candidate_manifest_out,
                authorization_path=args.authorization,
                timeout_secs=args.timeout_secs,
                runner_path=args.runner_path,
            )
        else:
            result = run_arm(args)
    except ExactCacheRunnerError as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
