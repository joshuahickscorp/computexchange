#!/usr/bin/env python3
"""CX-owned, fail-closed routing policy for inference acceleration.

This module is intentionally a control-plane boundary, not a model executor.
It decides only from already-bound request and evidence summaries.  The actual
engine remains replaceable (for example, the local vLLM-Metal fork), while CX
retains ownership of cache authorization, speculative-proposer selection,
fallback, and the audit trail that explains a route.

The routes are deliberately non-combinable performance lanes:

* ``exact_cache`` returns a response only when the *entire* pinned request and
  tenant scope match a verified exact-output artifact.
* ``prefix_cache_*`` returns a fresh completion after a verified shared-prefix
  KV hit.  It may then use a parity-safe speculative proposer or direct decode.
* ``ngram``, ``draft_model``, and ``mtp`` are fresh-decode routes and require
  their own exact-parity receipt.  They never inherit a cache multiplier.
* ``direct_decode`` is the safe target-model fallback for every valid request.

No status, confidence, or speedup supplied by a caller is trusted by itself:
every selected speculative path must be bound to the request's model,
tokenizer, runtime, lane, and an immutable receipt digest.  Invalid customer
requests are rejected without scheduling work; invalid/ineligible evidence
falls back to direct decode rather than being interpreted optimistically.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any, Mapping


SCHEMA_VERSION = 1
REQUEST_CONTRACT = "cx-inference-request-v1"

INTENT_EXPERIMENTAL = "experimental"
INTENT_FINAL = "final"
INTENTS = frozenset({INTENT_EXPERIMENTAL, INTENT_FINAL})

ROUTE_REJECTED_NO_EXECUTION = "rejected_no_execution"
ROUTE_EXACT_CACHE = "exact_cache"
ROUTE_PREFIX_CACHE_DIRECT = "prefix_cache_direct"
ROUTE_PREFIX_CACHE_NGRAM = "prefix_cache_ngram"
ROUTE_PREFIX_CACHE_DRAFT_MODEL = "prefix_cache_draft_model"
ROUTE_PREFIX_CACHE_MTP = "prefix_cache_mtp"
ROUTE_DIRECT_DECODE = "direct_decode"
ROUTE_NGRAM = "ngram"
ROUTE_DRAFT_MODEL = "draft_model"
ROUTE_MTP = "mtp"

SPECULATORS = ("ngram", "draft_model", "mtp")
FRESH_LANE = "fresh_decode"
PREFIX_LANE = "shared_prefix_reuse"

STATUS_CURRENT = "current"
STATUS_MISS = "miss"
STATUS_UNAVAILABLE = "unavailable"
STATUS_STALE = "stale"
STATUS_PARITY_FAILED = "parity_failed"
STATUS_REVOKED = "revoked"
STATUS_ERROR = "error"
STATUS_CORRUPTION = "corruption"
STATUSES = frozenset(
    {
        STATUS_CURRENT,
        STATUS_MISS,
        STATUS_UNAVAILABLE,
        STATUS_STALE,
        STATUS_PARITY_FAILED,
        STATUS_REVOKED,
        STATUS_ERROR,
        STATUS_CORRUPTION,
    }
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40,64}$")
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_REQUEST_KEYS = frozenset(
    {
        "contract",
        "request_identity_sha256",
        "tenant_scope_sha256",
        "model_id",
        "model_revision",
        "tokenizer_sha256",
        "runtime_sha256",
        "sampling_contract_sha256",
        "input_token_ids_sha256",
        "shared_prefix_token_ids_sha256",
        "shared_prefix_token_count",
        "max_output_tokens",
        "concurrency",
        "intent",
        "response_reuse_authorized",
        "prefix_reuse_authorized",
    }
)
_EVIDENCE_KEYS = frozenset({"exact_cache", "prefix_cache", "speculators"})
_EXACT_CACHE_KEYS = frozenset(
    {
        "status",
        "request_identity_sha256",
        "tenant_scope_sha256",
        "artifact_sha256",
        "output_token_ids_sha256",
        "target_output_token_ids_sha256",
        "artifact_verified",
        "exact_token_parity",
        "production_authorized",
        "direct_fallback_available",
    }
)
_PREFIX_CACHE_KEYS = frozenset(
    {
        "status",
        "tenant_scope_sha256",
        "model_revision",
        "tokenizer_sha256",
        "runtime_sha256",
        "prefix_token_ids_sha256",
        "prefix_token_count",
        "cache_entry_sha256",
        "cache_history_sha256",
        "integrity_verified",
        "production_authorized",
        "direct_fallback_available",
    }
)
_SPECULATOR_KEYS = frozenset(
    {
        "status",
        "receipt_sha256",
        "runtime_sha256",
        "model_revision",
        "tokenizer_sha256",
        "proposer_config_sha256",
        "verified_lane",
        "prefix_cache_allowed",
        "min_concurrency",
        "max_concurrency",
        "exact_token_parity",
        "repeat_stable",
        "production_authorized",
        "direct_fallback_available",
        "confidence",
        "minimum_confidence",
        "p50_speedup_x",
    }
)


class PolicyInputError(ValueError):
    """A request or evidence summary cannot safely select an accelerated path."""


def canonical_json_bytes(value: Any) -> bytes:
    """Canonical bytes used for request identity and policy audit bindings."""

    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def request_identity_sha256(request_without_identity: Mapping[str, Any]) -> str:
    """Compute the complete exact-response cache identity.

    The tenant scope, model/runtime pins, tokenized input, sampling contract,
    output cap, concurrency, and both reuse authorizations are deliberately in
    the key.  A caller cannot turn a prefix or response hit into a match by
    omitting one of those fields.
    """

    value = copy.deepcopy(dict(request_without_identity))
    value.pop("request_identity_sha256", None)
    return sha256_json(value)


def _require_dict(value: object, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PolicyInputError(f"{path} must be an object")
    return value


def _require_exact_keys(value: Mapping[str, Any], expected: frozenset[str], path: str) -> None:
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        raise PolicyInputError(f"{path} has unknown keys: {unknown}")
    if missing:
        raise PolicyInputError(f"{path} is missing keys: {missing}")


def _require_sha(value: object, path: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        suffix = " or null" if nullable else ""
        raise PolicyInputError(f"{path} must be a lowercase SHA-256{suffix}")
    return value


def _require_revision(value: object, path: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not _REVISION_RE.fullmatch(value):
        suffix = " or null" if nullable else ""
        raise PolicyInputError(f"{path} must be a full lowercase revision{suffix}")
    return value


def _require_string(value: object, path: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PolicyInputError(f"{path} must be a non-empty trimmed string")
    if len(value.encode("utf-8")) > maximum:
        raise PolicyInputError(f"{path} exceeds {maximum} UTF-8 bytes")
    return value


def _require_bool(value: object, path: str) -> bool:
    if type(value) is not bool:
        raise PolicyInputError(f"{path} must be boolean")
    return value


def _require_int(value: object, path: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise PolicyInputError(f"{path} must be an integer in [{minimum}, {maximum}]")
    return value


def _require_probability(value: object, path: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise PolicyInputError(f"{path} must be a finite number")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise PolicyInputError(f"{path} must be in [0, 1]")
    return number


def _require_positive_number(value: object, path: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise PolicyInputError(f"{path} must be a finite number")
    number = float(value)
    if number <= 0.0:
        raise PolicyInputError(f"{path} must be positive")
    return number


def _require_status(value: object, path: str) -> str:
    if not isinstance(value, str) or value not in STATUSES:
        raise PolicyInputError(f"{path} must be one of {sorted(STATUSES)}")
    return value


def _validate_request(request: object) -> dict[str, Any]:
    row = _require_dict(request, "request")
    _require_exact_keys(row, _REQUEST_KEYS, "request")
    if _require_string(row["contract"], "request.contract", maximum=128) != REQUEST_CONTRACT:
        raise PolicyInputError("request.contract is not supported")
    _require_sha(row["request_identity_sha256"], "request.request_identity_sha256")
    _require_sha(row["tenant_scope_sha256"], "request.tenant_scope_sha256")
    _require_string(row["model_id"], "request.model_id")
    _require_revision(row["model_revision"], "request.model_revision")
    _require_sha(row["tokenizer_sha256"], "request.tokenizer_sha256")
    _require_sha(row["runtime_sha256"], "request.runtime_sha256")
    _require_sha(row["sampling_contract_sha256"], "request.sampling_contract_sha256")
    _require_sha(row["input_token_ids_sha256"], "request.input_token_ids_sha256")
    prefix_sha = _require_sha(
        row["shared_prefix_token_ids_sha256"],
        "request.shared_prefix_token_ids_sha256",
        nullable=True,
    )
    prefix_count = _require_int(
        row["shared_prefix_token_count"],
        "request.shared_prefix_token_count",
        minimum=0,
        maximum=1_000_000,
    )
    if (prefix_sha is None) != (prefix_count == 0):
        raise PolicyInputError(
            "request shared_prefix token digest and count must be both present or both absent"
        )
    _require_int(row["max_output_tokens"], "request.max_output_tokens", minimum=1, maximum=1_000_000)
    _require_int(row["concurrency"], "request.concurrency", minimum=1, maximum=1_000_000)
    if row["intent"] not in INTENTS:
        raise PolicyInputError(f"request.intent must be one of {sorted(INTENTS)}")
    _require_bool(row["response_reuse_authorized"], "request.response_reuse_authorized")
    _require_bool(row["prefix_reuse_authorized"], "request.prefix_reuse_authorized")
    if row["request_identity_sha256"] != request_identity_sha256(row):
        raise PolicyInputError("request.request_identity_sha256 does not bind the complete request")
    return row


def _validate_exact_cache(value: object) -> dict[str, Any]:
    row = _require_dict(value, "evidence.exact_cache")
    _require_exact_keys(row, _EXACT_CACHE_KEYS, "evidence.exact_cache")
    _require_status(row["status"], "evidence.exact_cache.status")
    for field in (
        "request_identity_sha256",
        "tenant_scope_sha256",
        "artifact_sha256",
        "output_token_ids_sha256",
        "target_output_token_ids_sha256",
    ):
        _require_sha(row[field], f"evidence.exact_cache.{field}", nullable=True)
    for field in (
        "artifact_verified",
        "exact_token_parity",
        "production_authorized",
        "direct_fallback_available",
    ):
        _require_bool(row[field], f"evidence.exact_cache.{field}")
    return row


def _validate_prefix_cache(value: object) -> dict[str, Any]:
    row = _require_dict(value, "evidence.prefix_cache")
    _require_exact_keys(row, _PREFIX_CACHE_KEYS, "evidence.prefix_cache")
    _require_status(row["status"], "evidence.prefix_cache.status")
    _require_sha(row["tenant_scope_sha256"], "evidence.prefix_cache.tenant_scope_sha256", nullable=True)
    _require_revision(row["model_revision"], "evidence.prefix_cache.model_revision", nullable=True)
    for field in (
        "tokenizer_sha256",
        "runtime_sha256",
        "prefix_token_ids_sha256",
        "cache_entry_sha256",
        "cache_history_sha256",
    ):
        _require_sha(row[field], f"evidence.prefix_cache.{field}", nullable=True)
    _require_int(
        row["prefix_token_count"],
        "evidence.prefix_cache.prefix_token_count",
        minimum=0,
        maximum=1_000_000,
    )
    for field in ("integrity_verified", "production_authorized", "direct_fallback_available"):
        _require_bool(row[field], f"evidence.prefix_cache.{field}")
    return row


def _validate_speculator(kind: str, value: object) -> dict[str, Any]:
    row = _require_dict(value, f"evidence.speculators.{kind}")
    _require_exact_keys(row, _SPECULATOR_KEYS, f"evidence.speculators.{kind}")
    _require_status(row["status"], f"evidence.speculators.{kind}.status")
    for field in (
        "receipt_sha256",
        "runtime_sha256",
        "tokenizer_sha256",
        "proposer_config_sha256",
    ):
        _require_sha(row[field], f"evidence.speculators.{kind}.{field}", nullable=True)
    _require_revision(row["model_revision"], f"evidence.speculators.{kind}.model_revision", nullable=True)
    if row["verified_lane"] not in (FRESH_LANE, PREFIX_LANE, None):
        raise PolicyInputError(
            f"evidence.speculators.{kind}.verified_lane must be {FRESH_LANE!r}, {PREFIX_LANE!r}, or null"
        )
    _require_bool(row["prefix_cache_allowed"], f"evidence.speculators.{kind}.prefix_cache_allowed")
    minimum = _require_int(
        row["min_concurrency"],
        f"evidence.speculators.{kind}.min_concurrency",
        minimum=1,
        maximum=1_000_000,
    )
    maximum = _require_int(
        row["max_concurrency"],
        f"evidence.speculators.{kind}.max_concurrency",
        minimum=1,
        maximum=1_000_000,
    )
    if maximum < minimum:
        raise PolicyInputError(f"evidence.speculators.{kind} concurrency range is inverted")
    for field in (
        "exact_token_parity",
        "repeat_stable",
        "production_authorized",
        "direct_fallback_available",
    ):
        _require_bool(row[field], f"evidence.speculators.{kind}.{field}")
    confidence = _require_probability(row["confidence"], f"evidence.speculators.{kind}.confidence")
    threshold = _require_probability(
        row["minimum_confidence"],
        f"evidence.speculators.{kind}.minimum_confidence",
    )
    if threshold < 0.5:
        raise PolicyInputError(
            f"evidence.speculators.{kind}.minimum_confidence must not be below 0.5"
        )
    _require_positive_number(row["p50_speedup_x"], f"evidence.speculators.{kind}.p50_speedup_x")
    # Keep local names in the validation flow so static analyzers see the
    # values are deliberately parsed even though eligibility checks happen in
    # _speculator_reason below.
    del confidence, threshold
    return row


def _validate_evidence(evidence: object) -> dict[str, Any]:
    row = _require_dict(evidence, "evidence")
    _require_exact_keys(row, _EVIDENCE_KEYS, "evidence")
    _validate_exact_cache(row["exact_cache"])
    _validate_prefix_cache(row["prefix_cache"])
    speculators = _require_dict(row["speculators"], "evidence.speculators")
    _require_exact_keys(speculators, frozenset(SPECULATORS), "evidence.speculators")
    for kind in SPECULATORS:
        _validate_speculator(kind, speculators[kind])
    return row


def validate_policy_input(request: object, evidence: object) -> None:
    """Strictly validate a full request/evidence pair without routing it."""

    _validate_request(request)
    _validate_evidence(evidence)


def _exact_cache_reason(request: Mapping[str, Any], cache: Mapping[str, Any]) -> str | None:
    if cache["status"] != STATUS_CURRENT:
        return f"exact_cache_{cache['status']}"
    if not request["response_reuse_authorized"]:
        return "response_reuse_not_authorized"
    if cache["request_identity_sha256"] != request["request_identity_sha256"]:
        return "exact_cache_request_identity_mismatch"
    if cache["tenant_scope_sha256"] != request["tenant_scope_sha256"]:
        return "exact_cache_tenant_scope_mismatch"
    if not all(
        cache[field]
        for field in (
            "artifact_sha256",
            "output_token_ids_sha256",
            "target_output_token_ids_sha256",
            "artifact_verified",
            "exact_token_parity",
            "production_authorized",
            "direct_fallback_available",
        )
    ):
        return "exact_cache_not_production_safe"
    if cache["output_token_ids_sha256"] != cache["target_output_token_ids_sha256"]:
        return "exact_cache_output_parity_mismatch"
    return None


def _prefix_cache_reason(request: Mapping[str, Any], cache: Mapping[str, Any]) -> str | None:
    if cache["status"] != STATUS_CURRENT:
        return f"prefix_cache_{cache['status']}"
    if not request["prefix_reuse_authorized"]:
        return "prefix_reuse_not_authorized"
    if request["shared_prefix_token_ids_sha256"] is None:
        return "request_has_no_declared_shared_prefix"
    expected = {
        "tenant_scope_sha256": request["tenant_scope_sha256"],
        "model_revision": request["model_revision"],
        "tokenizer_sha256": request["tokenizer_sha256"],
        "runtime_sha256": request["runtime_sha256"],
        "prefix_token_ids_sha256": request["shared_prefix_token_ids_sha256"],
        "prefix_token_count": request["shared_prefix_token_count"],
    }
    for field, value in expected.items():
        if cache[field] != value:
            return f"prefix_cache_{field}_mismatch"
    if not all(
        cache[field]
        for field in (
            "cache_entry_sha256",
            "cache_history_sha256",
            "integrity_verified",
            "production_authorized",
            "direct_fallback_available",
        )
    ):
        return "prefix_cache_not_production_safe"
    return None


def _speculator_reason(
    request: Mapping[str, Any],
    speculator: Mapping[str, Any],
    *,
    kind: str,
    required_lane: str,
    prefix_active: bool,
) -> str | None:
    if speculator["status"] != STATUS_CURRENT:
        return f"speculator_{speculator['status']}"
    expected = {
        "runtime_sha256": request["runtime_sha256"],
        "model_revision": request["model_revision"],
        "tokenizer_sha256": request["tokenizer_sha256"],
    }
    for field, value in expected.items():
        if speculator[field] != value:
            return f"speculator_{field}_mismatch"
    if speculator["verified_lane"] != required_lane:
        return "speculator_lane_not_verified"
    # The recovered Metal C1/K3 regression occurred with prefix caching
    # explicitly disabled.  The fork therefore falls back for every
    # single-active n-gram step; do not let CX route around that source-level
    # safety boundary by treating a fresh C1 request as eligible.
    if kind == "ngram" and request["concurrency"] == 1:
        return "ngram_single_active_direct_only"
    if prefix_active and not speculator["prefix_cache_allowed"]:
        return "speculator_prefix_topology_not_safe"
    if not speculator["min_concurrency"] <= request["concurrency"] <= speculator["max_concurrency"]:
        return "speculator_concurrency_not_qualified"
    if not all(
        speculator[field]
        for field in (
            "receipt_sha256",
            "proposer_config_sha256",
            "exact_token_parity",
            "repeat_stable",
            "production_authorized",
            "direct_fallback_available",
        )
    ):
        return "speculator_not_parity_safe"
    if speculator["confidence"] < speculator["minimum_confidence"]:
        return "speculator_confidence_below_threshold"
    return None


def _route_for(kind: str, *, prefix_active: bool) -> str:
    routes = {
        "ngram": (ROUTE_NGRAM, ROUTE_PREFIX_CACHE_NGRAM),
        "draft_model": (ROUTE_DRAFT_MODEL, ROUTE_PREFIX_CACHE_DRAFT_MODEL),
        "mtp": (ROUTE_MTP, ROUTE_PREFIX_CACHE_MTP),
    }
    fresh, prefix = routes[kind]
    return prefix if prefix_active else fresh


def _decision(
    *,
    request: Mapping[str, Any] | None,
    attempted_route: str,
    delivered_route: str,
    execution_path: list[str],
    selected_speculator: str | None,
    evidence_receipt_sha256: str | None,
    fallback_reason: str | None,
) -> dict[str, Any]:
    cache_scope = (
        "exact_request"
        if delivered_route == ROUTE_EXACT_CACHE
        else "shared_prefix"
        if delivered_route.startswith("prefix_cache_")
        else "none"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "request_identity_sha256": (
            request["request_identity_sha256"] if request is not None else None
        ),
        "intent": request["intent"] if request is not None else None,
        "attempted_route": attempted_route,
        "delivered_route": delivered_route,
        "execution_path": execution_path,
        "cache_scope": cache_scope,
        "selected_speculator": selected_speculator,
        "evidence_receipt_sha256": evidence_receipt_sha256,
        "direct_fallback_route": ROUTE_DIRECT_DECODE,
        "fallback_reason": fallback_reason,
    }


def decide_inference_route(request: object, evidence: object) -> dict[str, Any]:
    """Select a single safe route for one request.

    Exact cache is considered first.  If it misses, a valid shared-prefix hit
    may be combined with the fastest *qualified* proposer.  Otherwise the
    policy considers only fresh-decode proposer receipts.  If no candidate has
    exact parity, repeat stability, binding, confidence, and direct fallback,
    direct target decode wins.  This never returns a blended multiplier.
    """

    try:
        request_row = _validate_request(request)
    except PolicyInputError:
        return _decision(
            request=None,
            attempted_route=ROUTE_REJECTED_NO_EXECUTION,
            delivered_route=ROUTE_REJECTED_NO_EXECUTION,
            execution_path=[],
            selected_speculator=None,
            evidence_receipt_sha256=None,
            fallback_reason="invalid_request_contract",
        )
    try:
        evidence_row = _validate_evidence(evidence)
    except PolicyInputError:
        return _decision(
            request=request_row,
            attempted_route=ROUTE_DIRECT_DECODE,
            delivered_route=ROUTE_DIRECT_DECODE,
            execution_path=[ROUTE_DIRECT_DECODE],
            selected_speculator=None,
            evidence_receipt_sha256=None,
            fallback_reason="invalid_policy_evidence",
        )

    exact_reason = _exact_cache_reason(request_row, evidence_row["exact_cache"])
    if exact_reason is None:
        return _decision(
            request=request_row,
            attempted_route=ROUTE_EXACT_CACHE,
            delivered_route=ROUTE_EXACT_CACHE,
            execution_path=["exact_response_cache"],
            selected_speculator=None,
            evidence_receipt_sha256=None,
            fallback_reason=None,
        )

    prefix_reason = _prefix_cache_reason(request_row, evidence_row["prefix_cache"])
    prefix_active = prefix_reason is None
    required_lane = PREFIX_LANE if prefix_active else FRESH_LANE

    eligible: list[tuple[float, str, Mapping[str, Any]]] = []
    speculator_reasons: list[str] = []
    for kind in SPECULATORS:
        candidate = evidence_row["speculators"][kind]
        reason = _speculator_reason(
            request_row,
            candidate,
            kind=kind,
            required_lane=required_lane,
            prefix_active=prefix_active,
        )
        if reason is None:
            # Higher measured p50 is only a deterministic tie-breaker between
            # individually eligible receipts; it is never added to cache gain.
            eligible.append((float(candidate["p50_speedup_x"]), kind, candidate))
        else:
            speculator_reasons.append(reason)

    if eligible:
        # Stable lexical second key makes the result deterministic when receipts
        # report the same p50 speedup.
        _, kind, selected = max(eligible, key=lambda row: (row[0], row[1]))
        route = _route_for(kind, prefix_active=prefix_active)
        path = (["prefix_kv_cache"] if prefix_active else []) + [kind]
        return _decision(
            request=request_row,
            attempted_route=route,
            delivered_route=route,
            execution_path=path,
            selected_speculator=kind,
            evidence_receipt_sha256=selected["receipt_sha256"],
            fallback_reason=None,
        )

    if prefix_active:
        return _decision(
            request=request_row,
            attempted_route=ROUTE_PREFIX_CACHE_DIRECT,
            delivered_route=ROUTE_PREFIX_CACHE_DIRECT,
            execution_path=["prefix_kv_cache", ROUTE_DIRECT_DECODE],
            selected_speculator=None,
            evidence_receipt_sha256=None,
            fallback_reason="no_parity_safe_prefix_speculator",
        )

    # Do not expose attacker-controlled details or the complete candidate
    # reason list to a customer.  The raw evidence remains available to the
    # receipt/audit layer; the route gets a stable safety classification.
    del speculator_reasons
    return _decision(
        request=request_row,
        attempted_route=ROUTE_DIRECT_DECODE,
        delivered_route=ROUTE_DIRECT_DECODE,
        execution_path=[ROUTE_DIRECT_DECODE],
        selected_speculator=None,
        evidence_receipt_sha256=None,
        fallback_reason=(
            "no_parity_safe_fresh_speculator"
            if prefix_reason is not None
            else "no_parity_safe_speculator"
        ),
    )


__all__ = [
    "FRESH_LANE",
    "INTENT_EXPERIMENTAL",
    "INTENT_FINAL",
    "PREFIX_LANE",
    "PolicyInputError",
    "REQUEST_CONTRACT",
    "ROUTE_DIRECT_DECODE",
    "ROUTE_DRAFT_MODEL",
    "ROUTE_EXACT_CACHE",
    "ROUTE_MTP",
    "ROUTE_NGRAM",
    "ROUTE_PREFIX_CACHE_DIRECT",
    "ROUTE_PREFIX_CACHE_DRAFT_MODEL",
    "ROUTE_PREFIX_CACHE_MTP",
    "ROUTE_PREFIX_CACHE_NGRAM",
    "ROUTE_REJECTED_NO_EXECUTION",
    "SCHEMA_VERSION",
    "SPECULATORS",
    "canonical_json_bytes",
    "decide_inference_route",
    "request_identity_sha256",
    "sha256_json",
    "validate_policy_input",
]
