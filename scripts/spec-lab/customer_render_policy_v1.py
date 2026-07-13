#!/usr/bin/env python3
"""Pure, lab-only customer routing policy for speculative render evidence.

This module deliberately does not execute a render, read a receipt, or mutate
production state.  It accepts already-verified evidence summaries and returns a
deterministic routing decision.  The public decision entry point is fail closed:
an invalid customer request contract selects ``rejected_no_execution``.  A valid
request whose speculative evidence is invalid still falls back to
``basic_cycles`` with the request's original billing ceiling.

One-render and temporal experiments are intentionally absent from the input
schema.  They are reported as audit-only routes and can never be selected by
this policy version.
"""

from __future__ import annotations

from typing import Any


SCHEMA_VERSION = 1

INTENT_EXPERIMENTAL_PREVIEW = "experimental_preview"
INTENT_FINAL = "final"
INTENTS = frozenset({INTENT_EXPERIMENTAL_PREVIEW, INTENT_FINAL})

ROUTE_BASIC_CYCLES = "basic_cycles"
ROUTE_EXACT_CACHE = "exact_cache"
ROUTE_SPATIAL75 = "spatial75"
ROUTE_REJECTED_NO_EXECUTION = "rejected_no_execution"
AUDIT_ONLY_ROUTES = ("one_render_upper_bound", "temporal_prediction")

STATUS_CURRENT = "current"
STATUS_UNAVAILABLE = "unavailable"
STATUS_STALE = "stale"
STATUS_ERROR = "error"
STATUS_TIMEOUT = "timeout"
STATUS_CORRUPTION = "corruption"
STATUS_FAILURE = "failure"
EVIDENCE_STATUSES = frozenset(
    {
        STATUS_CURRENT,
        STATUS_UNAVAILABLE,
        STATUS_STALE,
        STATUS_ERROR,
        STATUS_TIMEOUT,
        STATUS_CORRUPTION,
        STATUS_FAILURE,
    }
)
FATAL_EVIDENCE_STATUSES = frozenset(
    {STATUS_ERROR, STATUS_TIMEOUT, STATUS_CORRUPTION, STATUS_FAILURE}
)

BILLABILITY_NON_BILLABLE = "non_billable"
BILLABILITY_STANDARD_CUSTOMER_RENDER = "standard_customer_render"

_REQUEST_KEYS = frozenset({"intent", "request_identity"})
_EVIDENCE_KEYS = frozenset({"exact_cache", "spatial75"})
_EXACT_CACHE_KEYS = frozenset(
    {"evidence_status", "artifact_request_identity", "source_eligibility"}
)
_SOURCE_ELIGIBILITY_KEYS = frozenset(
    {
        "experimental_preview",
        "production_ready",
        "artifact_verified",
        "billing_eligible",
    }
)
_SPATIAL75_KEYS = frozenset(
    {"evidence_status", "request_identity", "fresh_two_render_gate_pass"}
)


class PolicyInputError(ValueError):
    """Raised by strict validation for an invalid policy input."""


def _require_dict(value: object, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PolicyInputError(f"{path} must be a dict")
    return value


def _require_exact_keys(value: dict[str, Any], expected: frozenset[str], path: str) -> None:
    actual = frozenset(value)
    unknown = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unknown:
        raise PolicyInputError(f"{path} has unknown keys: {unknown}")
    if missing:
        raise PolicyInputError(f"{path} is missing keys: {missing}")


def _require_enum(value: object, allowed: frozenset[str], path: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise PolicyInputError(f"{path} must be one of {sorted(allowed)}")
    return value


def _require_bool(value: object, path: str) -> bool:
    # bool is a subclass of int, so an isinstance(value, int) check is unsafe.
    if type(value) is not bool:
        raise PolicyInputError(f"{path} must be a bool")
    return value


def _require_identity(value: object, path: str, *, nullable: bool) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        suffix = " or null" if nullable else ""
        raise PolicyInputError(f"{path} must be a string{suffix}")
    if not value or value != value.strip() or len(value) > 512:
        raise PolicyInputError(
            f"{path} must be a non-empty, trimmed string of at most 512 characters"
        )
    return value


def _validate_request(request: object) -> tuple[str, str]:
    request_dict = _require_dict(request, "request")
    _require_exact_keys(request_dict, _REQUEST_KEYS, "request")
    intent = _require_enum(request_dict["intent"], INTENTS, "request.intent")
    identity = _require_identity(
        request_dict["request_identity"],
        "request.request_identity",
        nullable=False,
    )
    assert identity is not None
    return intent, identity


def _validate_evidence(evidence: object) -> None:
    evidence_dict = _require_dict(evidence, "evidence")
    _require_exact_keys(evidence_dict, _EVIDENCE_KEYS, "evidence")

    exact_cache = _require_dict(evidence_dict["exact_cache"], "evidence.exact_cache")
    _require_exact_keys(exact_cache, _EXACT_CACHE_KEYS, "evidence.exact_cache")
    _require_enum(
        exact_cache["evidence_status"],
        EVIDENCE_STATUSES,
        "evidence.exact_cache.evidence_status",
    )
    _require_identity(
        exact_cache["artifact_request_identity"],
        "evidence.exact_cache.artifact_request_identity",
        nullable=True,
    )
    eligibility = _require_dict(
        exact_cache["source_eligibility"],
        "evidence.exact_cache.source_eligibility",
    )
    _require_exact_keys(
        eligibility,
        _SOURCE_ELIGIBILITY_KEYS,
        "evidence.exact_cache.source_eligibility",
    )
    for key in sorted(_SOURCE_ELIGIBILITY_KEYS):
        _require_bool(
            eligibility[key],
            f"evidence.exact_cache.source_eligibility.{key}",
        )
    if eligibility["production_ready"] and not eligibility["artifact_verified"]:
        raise PolicyInputError(
            "production_ready source eligibility requires artifact_verified"
        )
    if eligibility["billing_eligible"] and not (
        eligibility["production_ready"] and eligibility["artifact_verified"]
    ):
        raise PolicyInputError(
            "billing_eligible source eligibility requires a verified production artifact"
        )

    spatial75 = _require_dict(evidence_dict["spatial75"], "evidence.spatial75")
    _require_exact_keys(spatial75, _SPATIAL75_KEYS, "evidence.spatial75")
    _require_enum(
        spatial75["evidence_status"],
        EVIDENCE_STATUSES,
        "evidence.spatial75.evidence_status",
    )
    _require_identity(
        spatial75["request_identity"],
        "evidence.spatial75.request_identity",
        nullable=True,
    )
    _require_bool(
        spatial75["fresh_two_render_gate_pass"],
        "evidence.spatial75.fresh_two_render_gate_pass",
    )


def validate_policy_input(request: object, evidence: object) -> None:
    """Strictly validate the complete policy schema.

    This lower-level API raises :class:`PolicyInputError`, making schema
    problems observable in tests and at integration boundaries.  Callers that
    must never throw should use :func:`decide_customer_render`, which rejects an
    invalid request contract without selecting an executable route and converts
    invalid speculative evidence for a valid request into a basic-Cycles
    fallback.
    """

    _validate_request(request)
    _validate_evidence(evidence)


def _route_order(intent: str | None) -> list[str]:
    if intent == INTENT_EXPERIMENTAL_PREVIEW:
        return [ROUTE_EXACT_CACHE, ROUTE_SPATIAL75, ROUTE_BASIC_CYCLES]
    if intent == INTENT_FINAL:
        return [ROUTE_EXACT_CACHE, ROUTE_BASIC_CYCLES]
    return [ROUTE_REJECTED_NO_EXECUTION]


def _decision(
    *,
    intent: str | None,
    attempted_route: str,
    delivered_route: str,
    fallback_reason: str | None,
) -> dict[str, object]:
    # Experimental intent can never create a billable result, including when
    # it falls back to basic Cycles. Rejected requests are capped the same way.
    billability_ceiling = (
        BILLABILITY_STANDARD_CUSTOMER_RENDER
        if intent == INTENT_FINAL
        else BILLABILITY_NON_BILLABLE
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "request_intent": intent,
        "attempted_route": attempted_route,
        "delivered_route": delivered_route,
        "fallback_reason": fallback_reason,
        "billability_ceiling": billability_ceiling,
        "route_order": _route_order(intent),
        "audit_only_routes": list(AUDIT_ONLY_ROUTES),
    }


def _basic_fallback(
    intent: str | None,
    attempted_route: str,
    reason: str,
) -> dict[str, object]:
    return _decision(
        intent=intent,
        attempted_route=attempted_route,
        delivered_route=ROUTE_BASIC_CYCLES,
        fallback_reason=reason,
    )


def _reject_invalid_request() -> dict[str, object]:
    return _decision(
        intent=None,
        attempted_route=ROUTE_REJECTED_NO_EXECUTION,
        delivered_route=ROUTE_REJECTED_NO_EXECUTION,
        fallback_reason="invalid_request_contract",
    )


def decide_customer_render(request: object, evidence: object) -> dict[str, object]:
    """Return a deterministic, customer-safe routing decision.

    Route ordering is exact cache, then (for explicit experimental previews)
    Spatial75, then basic Cycles.  A fatal status on the route currently being
    considered stops experimental routing immediately.  Lower-priority route
    failures are irrelevant after a higher-priority route has been selected.

    Input validation failures are intentionally not reflected verbatim in the
    decision, avoiding unstable or attacker-controlled diagnostic text.
    """

    try:
        intent, request_identity = _validate_request(request)
    except PolicyInputError:
        # A malformed customer contract is not a render request.  Keep the
        # rejection structurally distinct from every executable fallback so a
        # caller cannot accidentally schedule free or unbound Cycles work.
        return _reject_invalid_request()
    try:
        _validate_evidence(evidence)
    except PolicyInputError:
        # Preserve a fully valid customer intent across an internal evidence
        # failure. A final request still falls back to ordinary billable
        # Cycles; invalid request contracts were already rejected above.
        return _basic_fallback(
            intent,
            ROUTE_BASIC_CYCLES,
            "invalid_policy_input",
        )

    # Validation above establishes these exact shapes and types.
    request_dict = request
    evidence_dict = evidence
    exact_cache = evidence_dict["exact_cache"]
    spatial75 = evidence_dict["spatial75"]

    exact_status = exact_cache["evidence_status"]
    if exact_status in FATAL_EVIDENCE_STATUSES:
        return _basic_fallback(
            intent,
            ROUTE_EXACT_CACHE,
            f"exact_cache_{exact_status}",
        )

    exact_current = exact_status == STATUS_CURRENT
    exact_identity_match = (
        exact_current
        and exact_cache["artifact_request_identity"] == request_identity
    )
    eligibility = exact_cache["source_eligibility"]
    exact_final_eligible = all(
        eligibility[key]
        for key in ("production_ready", "artifact_verified", "billing_eligible")
    )

    if intent == INTENT_FINAL:
        if exact_identity_match and exact_final_eligible:
            return _decision(
                intent=intent,
                attempted_route=ROUTE_EXACT_CACHE,
                delivered_route=ROUTE_EXACT_CACHE,
                fallback_reason=None,
            )
        if not exact_current:
            reason = "exact_cache_not_current"
        elif not exact_identity_match:
            reason = "exact_cache_identity_mismatch"
        else:
            reason = "exact_cache_not_final_eligible"
        return _basic_fallback(intent, ROUTE_EXACT_CACHE, reason)

    # A higher-tier final artifact may satisfy a preview request.  The preview
    # request's non-billable ceiling still applies, so this cannot escalate a
    # customer charge merely because the cached source has stronger eligibility.
    if exact_identity_match and (
        eligibility["experimental_preview"] or exact_final_eligible
    ):
        return _decision(
            intent=intent,
            attempted_route=ROUTE_EXACT_CACHE,
            delivered_route=ROUTE_EXACT_CACHE,
            fallback_reason=None,
        )

    spatial_status = spatial75["evidence_status"]
    if spatial_status in FATAL_EVIDENCE_STATUSES:
        return _basic_fallback(
            intent,
            ROUTE_SPATIAL75,
            f"spatial75_{spatial_status}",
        )
    if (
        spatial_status == STATUS_CURRENT
        and spatial75["request_identity"] == request_identity
        and spatial75["fresh_two_render_gate_pass"]
    ):
        return _decision(
            intent=intent,
            attempted_route=ROUTE_SPATIAL75,
            delivered_route=ROUTE_SPATIAL75,
            fallback_reason=None,
        )
    if spatial_status != STATUS_CURRENT:
        reason = "spatial75_evidence_not_current"
    elif spatial75["request_identity"] != request_identity:
        reason = "spatial75_identity_mismatch"
    else:
        reason = "spatial75_gate_not_passed"
    return _basic_fallback(intent, ROUTE_SPATIAL75, reason)


__all__ = [
    "AUDIT_ONLY_ROUTES",
    "BILLABILITY_NON_BILLABLE",
    "BILLABILITY_STANDARD_CUSTOMER_RENDER",
    "EVIDENCE_STATUSES",
    "FATAL_EVIDENCE_STATUSES",
    "INTENT_EXPERIMENTAL_PREVIEW",
    "INTENT_FINAL",
    "PolicyInputError",
    "ROUTE_BASIC_CYCLES",
    "ROUTE_EXACT_CACHE",
    "ROUTE_REJECTED_NO_EXECUTION",
    "ROUTE_SPATIAL75",
    "SCHEMA_VERSION",
    "decide_customer_render",
    "validate_policy_input",
]
