#!/usr/bin/env python3
"""Fail-closed receipt contract for the CX 50x inference lanes.

This module deliberately keeps three acceleration mechanisms separate:

* ``exact_request_reuse`` is a byte/token-identical response-cache hit;
* ``shared_prefix_reuse`` is a fresh completion after a cached shared prefix;
* ``fresh_decode`` has no response or prefix reuse at all.

It validates a self-hashed receipt, recomputes its workload digest and timing
statistics from raw ABBA samples, requires exact output-token parity, and can
also hash every externally retained capture.  A valid receipt is evidence for
only its declared lane; it does not combine cache, batching, prefix, or
speculative-decode multipliers, and it does not by itself authorize a product
or price claim.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
RECORD_KIND = "cx_inference_50x_lane_receipt"
MAX_RECEIPT_BYTES = 4 << 20
MAX_ARTIFACT_BYTES = 128 << 20
MIN_TRIALS_PER_ARM = 8
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

LANES = {
    "exact_request_reuse": {
        "scorecard_lane_id": "inference.exact-request-reuse",
        "scope": "exact_request",
        "candidate_cache_state": "exact_response_cache_hit",
    },
    "shared_prefix_reuse": {
        "scorecard_lane_id": "inference.shared-prefix-reuse",
        "scope": "shared_prefix",
        "candidate_cache_state": "prefix_kv_cache_hit",
    },
    "fresh_decode": {
        "scorecard_lane_id": "inference.fresh-decode",
        "scope": "none",
        "candidate_cache_state": "disabled",
    },
}
ABBA = ("baseline", "candidate", "candidate", "baseline")
STAGES = (
    "admission",
    "routing",
    "cache_lookup",
    "engine_execution",
    "verification",
    "serialization",
    "delivery",
)
AUTHORIZATION_FIELDS = (
    "artifact_verified",
    "customer_selectable",
    "publication_eligible",
    "production_ready",
    "billing_eligible",
)
ARTIFACT_KINDS = {"sample_capture", "parity_audit", "quality_audit"}
EVIDENCE_CLASSES = {
    "physical_independently_attested",
    "physical_local_unattested",
}
NUMERIC_TOLERANCE = 0.000001


class InferenceReceiptError(ValueError):
    """The receipt cannot support the requested inference-lane conclusion."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return the canonical encoding used by every receipt hash."""

    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def receipt_sha256(value: Mapping[str, Any]) -> str:
    """Hash a receipt excluding its self-reference field."""

    unsigned = copy.deepcopy(dict(value))
    unsigned.pop("receipt_sha256", None)
    return sha256_json(unsigned)


def _duplicate_safe_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InferenceReceiptError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_json_bytes(raw: bytes, *, context: str = "receipt") -> dict[str, Any]:
    if not raw:
        raise InferenceReceiptError(f"{context} is empty")
    if len(raw) > MAX_RECEIPT_BYTES:
        raise InferenceReceiptError(f"{context} exceeds {MAX_RECEIPT_BYTES} bytes")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_duplicate_safe_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                InferenceReceiptError(f"{context} contains non-finite JSON value {token!r}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InferenceReceiptError(f"{context} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise InferenceReceiptError(f"{context} root must be an object")
    return value


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InferenceReceiptError(f"{context} must be an object")
    return value


def _exact(value: Mapping[str, Any], fields: set[str], context: str) -> None:
    missing = sorted(fields - set(value))
    extra = sorted(set(value) - fields)
    if missing:
        raise InferenceReceiptError(f"{context} missing field(s): {', '.join(missing)}")
    if extra:
        raise InferenceReceiptError(f"{context} has unknown field(s): {', '.join(extra)}")


def _string(value: Any, context: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InferenceReceiptError(f"{context} must be a non-empty string")
    if len(value.encode("utf-8")) > maximum:
        raise InferenceReceiptError(f"{context} exceeds {maximum} UTF-8 bytes")
    return value


def _identifier(value: Any, context: str) -> str:
    result = _string(value, context, maximum=128)
    if not IDENTIFIER_RE.fullmatch(result):
        raise InferenceReceiptError(f"{context} must be a lowercase identifier")
    return result


def _sha256(value: Any, context: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise InferenceReceiptError(f"{context} must be a lowercase SHA-256")
    return value


def _number(value: Any, context: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InferenceReceiptError(f"{context} must be a finite number")
    result = float(value)
    if result < minimum:
        raise InferenceReceiptError(f"{context} must be >= {minimum}")
    return result


def _integer(value: Any, context: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise InferenceReceiptError(f"{context} must be an integer >= {minimum}")
    return value


def _boolean(value: Any, context: str) -> bool:
    if type(value) is not bool:
        raise InferenceReceiptError(f"{context} must be boolean")
    return value


def _relative_path(value: Any, context: str) -> str:
    result = _string(value, context, maximum=512)
    path = Path(result)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise InferenceReceiptError(f"{context} must be a relative artifact path")
    return result


def _same_float(actual: float, expected: float, context: str) -> None:
    if not math.isclose(actual, expected, rel_tol=1e-9, abs_tol=NUMERIC_TOLERANCE):
        raise InferenceReceiptError(f"{context} does not match recomputed value")


def _validate_workload(value: Any) -> dict[str, Any]:
    row = _object(value, "receipt.workload")
    _exact(
        row,
        {
            "contract",
            "request_set_sha256",
            "request_order_sha256",
            "request_count",
            "max_output_tokens",
            "sampling",
            "concurrency",
            "customer_visible_stages",
        },
        "receipt.workload",
    )
    if _string(row["contract"], "receipt.workload.contract", maximum=160) != (
        "cx-openai-greedy-completions-v1"
    ):
        raise InferenceReceiptError("receipt.workload.contract is not the pinned greedy contract")
    _sha256(row["request_set_sha256"], "receipt.workload.request_set_sha256")
    _sha256(row["request_order_sha256"], "receipt.workload.request_order_sha256")
    _integer(row["request_count"], "receipt.workload.request_count", minimum=1)
    _integer(row["max_output_tokens"], "receipt.workload.max_output_tokens", minimum=1)
    _integer(row["concurrency"], "receipt.workload.concurrency", minimum=1)
    sampling = _object(row["sampling"], "receipt.workload.sampling")
    _exact(sampling, {"temperature", "top_p", "seed", "n"}, "receipt.workload.sampling")
    if _number(sampling["temperature"], "receipt.workload.sampling.temperature") != 0:
        raise InferenceReceiptError("receipt.workload.sampling.temperature must be 0 for exact parity")
    if _number(sampling["top_p"], "receipt.workload.sampling.top_p") != 1:
        raise InferenceReceiptError("receipt.workload.sampling.top_p must be 1 for exact parity")
    _integer(sampling["seed"], "receipt.workload.sampling.seed", minimum=0)
    if _integer(sampling["n"], "receipt.workload.sampling.n", minimum=1) != 1:
        raise InferenceReceiptError("receipt.workload.sampling.n must be 1")
    if row["customer_visible_stages"] != list(STAGES):
        raise InferenceReceiptError("receipt.workload.customer_visible_stages must list every charged stage")
    return copy.deepcopy(row)


def _validate_runtime(value: Any) -> dict[str, Any]:
    row = _object(value, "receipt.runtime")
    _exact(
        row,
        {
            "engine_id",
            "engine_commit",
            "metal_runtime_sha256",
            "model_id",
            "model_revision",
            "weights_sha256",
            "tokenizer_sha256",
            "precision_id",
            "baseline_config_sha256",
            "candidate_config_sha256",
        },
        "receipt.runtime",
    )
    _identifier(row["engine_id"], "receipt.runtime.engine_id")
    for field in ("engine_commit", "model_revision"):
        candidate = _string(row[field], f"receipt.runtime.{field}", maximum=64)
        if not COMMIT_RE.fullmatch(candidate):
            raise InferenceReceiptError(f"receipt.runtime.{field} must be a full pinned commit")
    _string(row["model_id"], "receipt.runtime.model_id", maximum=256)
    _identifier(row["precision_id"], "receipt.runtime.precision_id")
    for field in (
        "metal_runtime_sha256",
        "weights_sha256",
        "tokenizer_sha256",
        "baseline_config_sha256",
        "candidate_config_sha256",
    ):
        _sha256(row[field], f"receipt.runtime.{field}")
    return copy.deepcopy(row)


def _validate_comparison(value: Any) -> dict[str, Any]:
    row = _object(value, "receipt.comparison")
    _exact(
        row,
        {
            "claim_axis",
            "timing_scope",
            "same_logical_work",
            "baseline_mode",
            "candidate_mode",
            "target_multiplier",
        },
        "receipt.comparison",
    )
    if row["claim_axis"] != "inference_request_turnaround":
        raise InferenceReceiptError("receipt.comparison.claim_axis must be inference_request_turnaround")
    if row["timing_scope"] != "integrated_wall":
        raise InferenceReceiptError("receipt.comparison.timing_scope must be integrated_wall")
    if not _boolean(row["same_logical_work"], "receipt.comparison.same_logical_work"):
        raise InferenceReceiptError("receipt.comparison.same_logical_work must be true")
    if row["baseline_mode"] != "target_only":
        raise InferenceReceiptError("receipt.comparison.baseline_mode must be target_only")
    candidate_mode = _identifier(row["candidate_mode"], "receipt.comparison.candidate_mode")
    if candidate_mode == "target_only":
        raise InferenceReceiptError("receipt.comparison.candidate_mode cannot be target_only")
    _number(row["target_multiplier"], "receipt.comparison.target_multiplier", minimum=1.0)
    return copy.deepcopy(row)


def _validate_scorecard_binding(value: Any, lane: str) -> dict[str, Any]:
    row = _object(value, "receipt.scorecard_binding")
    _exact(row, {"lane_id", "display_name", "comparison_group"}, "receipt.scorecard_binding")
    expected_lane_id = LANES[lane]["scorecard_lane_id"]
    if row["lane_id"] != expected_lane_id:
        raise InferenceReceiptError(
            f"receipt.scorecard_binding.lane_id must be {expected_lane_id} for {lane}"
        )
    _string(row["display_name"], "receipt.scorecard_binding.display_name", maximum=160)
    _identifier(row["comparison_group"], "receipt.scorecard_binding.comparison_group")
    return copy.deepcopy(row)


def _validate_coverage(value: Any) -> dict[str, Any]:
    row = _object(value, "receipt.reuse.coverage")
    _exact(
        row,
        {
            "coverage_workload_sha256",
            "observed_requests",
            "eligible_requests",
            "eligible_hits",
            "eligible_request_fraction",
            "eligible_hit_rate",
            "required_eligible_hit_rate",
        },
        "receipt.reuse.coverage",
    )
    _sha256(row["coverage_workload_sha256"], "receipt.reuse.coverage.coverage_workload_sha256")
    observed = _integer(row["observed_requests"], "receipt.reuse.coverage.observed_requests", minimum=1)
    eligible = _integer(row["eligible_requests"], "receipt.reuse.coverage.eligible_requests", minimum=1)
    hits = _integer(row["eligible_hits"], "receipt.reuse.coverage.eligible_hits", minimum=0)
    if eligible > observed or hits > eligible:
        raise InferenceReceiptError("receipt.reuse.coverage request counts are inconsistent")
    eligible_fraction = _number(
        row["eligible_request_fraction"], "receipt.reuse.coverage.eligible_request_fraction"
    )
    hit_rate = _number(row["eligible_hit_rate"], "receipt.reuse.coverage.eligible_hit_rate")
    required_rate = _number(
        row["required_eligible_hit_rate"],
        "receipt.reuse.coverage.required_eligible_hit_rate",
    )
    if any(rate > 1 for rate in (eligible_fraction, hit_rate, required_rate)):
        raise InferenceReceiptError("receipt.reuse.coverage rates must be <= 1")
    _same_float(eligible_fraction, eligible / observed, "receipt.reuse.coverage.eligible_request_fraction")
    _same_float(hit_rate, hits / eligible, "receipt.reuse.coverage.eligible_hit_rate")
    if hit_rate < required_rate:
        raise InferenceReceiptError(
            "receipt.reuse.coverage.eligible_hit_rate is below required_eligible_hit_rate"
        )
    return copy.deepcopy(row)


def _validate_reuse(value: Any, lane: str) -> dict[str, Any]:
    row = _object(value, "receipt.reuse")
    _exact(
        row,
        {
            "scope",
            "baseline_cache_state",
            "candidate_cache_state",
            "exact_response_reuse",
            "shared_prefix_tokens",
            "coverage",
        },
        "receipt.reuse",
    )
    expected = LANES[lane]
    if row["scope"] != expected["scope"]:
        raise InferenceReceiptError("receipt.reuse.scope does not match receipt.lane")
    if row["baseline_cache_state"] != "disabled":
        raise InferenceReceiptError("receipt.reuse.baseline_cache_state must be disabled")
    if row["candidate_cache_state"] != expected["candidate_cache_state"]:
        raise InferenceReceiptError("receipt.reuse.candidate_cache_state does not match receipt.lane")
    exact_reuse = _boolean(row["exact_response_reuse"], "receipt.reuse.exact_response_reuse")
    prefix_tokens = _integer(row["shared_prefix_tokens"], "receipt.reuse.shared_prefix_tokens")
    coverage = row["coverage"]
    if lane == "exact_request_reuse":
        if not exact_reuse or prefix_tokens != 0 or coverage is None:
            raise InferenceReceiptError("exact_request_reuse requires exact reuse, no prefix count, and coverage")
        _validate_coverage(coverage)
    elif lane == "shared_prefix_reuse":
        if exact_reuse or prefix_tokens < 1 or coverage is None:
            raise InferenceReceiptError("shared_prefix_reuse requires a positive prefix count and coverage")
        _validate_coverage(coverage)
    else:
        if exact_reuse or prefix_tokens != 0 or coverage is not None:
            raise InferenceReceiptError("fresh_decode forbids response/prefix reuse and coverage")
    return copy.deepcopy(row)


def _validate_fallback(value: Any) -> dict[str, Any]:
    row = _object(value, "receipt.fallback")
    _exact(row, {"enabled", "mode", "trigger", "validated"}, "receipt.fallback")
    if not _boolean(row["enabled"], "receipt.fallback.enabled"):
        raise InferenceReceiptError("receipt.fallback.enabled must be true")
    if row["mode"] != "target_only_direct_decode":
        raise InferenceReceiptError("receipt.fallback.mode must be target_only_direct_decode")
    if row["trigger"] != "parity_or_confidence_failure":
        raise InferenceReceiptError("receipt.fallback.trigger must be parity_or_confidence_failure")
    if not _boolean(row["validated"], "receipt.fallback.validated"):
        raise InferenceReceiptError("receipt.fallback.validated must be true")
    return copy.deepcopy(row)


def _validate_authorization(value: Any) -> dict[str, bool]:
    row = _object(value, "receipt.authorization")
    _exact(row, set(AUTHORIZATION_FIELDS), "receipt.authorization")
    return {field: _boolean(row[field], f"receipt.authorization.{field}") for field in AUTHORIZATION_FIELDS}


def _validate_attestation(value: Any) -> dict[str, Any]:
    row = _object(value, "receipt.attestation")
    _exact(row, {"evidence_class", "independent_attestation"}, "receipt.attestation")
    evidence_class = _string(row["evidence_class"], "receipt.attestation.evidence_class")
    if evidence_class not in EVIDENCE_CLASSES:
        raise InferenceReceiptError("receipt.attestation.evidence_class is not promotable evidence")
    independently_attested = _boolean(
        row["independent_attestation"], "receipt.attestation.independent_attestation"
    )
    if independently_attested != (evidence_class == "physical_independently_attested"):
        raise InferenceReceiptError("receipt.attestation fields disagree")
    return copy.deepcopy(row)


def _validate_artifacts(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise InferenceReceiptError("receipt.artifacts must be a non-empty list")
    artifacts: dict[str, dict[str, Any]] = {}
    paths: set[str] = set()
    for index, raw in enumerate(value):
        row = _object(raw, f"receipt.artifacts[{index}]")
        _exact(row, {"artifact_id", "kind", "path", "sha256"}, f"receipt.artifacts[{index}]")
        artifact_id = _identifier(row["artifact_id"], f"receipt.artifacts[{index}].artifact_id")
        if artifact_id in artifacts:
            raise InferenceReceiptError("receipt.artifacts has duplicate artifact_id")
        kind = _identifier(row["kind"], f"receipt.artifacts[{index}].kind")
        if kind not in ARTIFACT_KINDS:
            raise InferenceReceiptError("receipt.artifacts kind is not recognized")
        path = _relative_path(row["path"], f"receipt.artifacts[{index}].path")
        if path in paths:
            raise InferenceReceiptError("receipt.artifacts has duplicate path")
        paths.add(path)
        artifacts[artifact_id] = {
            "artifact_id": artifact_id,
            "kind": kind,
            "path": path,
            "sha256": _sha256(row["sha256"], f"receipt.artifacts[{index}].sha256"),
        }
    return artifacts


def _validate_sample(
    value: Any,
    *,
    index: int,
    workload_sha256: str,
    request_count: int,
    artifacts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    context = f"receipt.samples[{index}]"
    row = _object(value, context)
    _exact(
        row,
        {
            "order",
            "sample_id",
            "arm",
            "workload_sha256",
            "request_count",
            "elapsed_ms",
            "stages_ms",
            "output_token_ids_sha256",
            "output_token_count",
            "quality_sha256",
            "capture_artifact_id",
            "status",
        },
        context,
    )
    if _integer(row["order"], f"{context}.order") != index:
        raise InferenceReceiptError(f"{context}.order must be contiguous and zero-based")
    sample_id = _identifier(row["sample_id"], f"{context}.sample_id")
    arm = row["arm"]
    if arm not in {"baseline", "candidate"}:
        raise InferenceReceiptError(f"{context}.arm must be baseline or candidate")
    if _sha256(row["workload_sha256"], f"{context}.workload_sha256") != workload_sha256:
        raise InferenceReceiptError(f"{context}.workload_sha256 does not bind the receipt workload")
    if _integer(row["request_count"], f"{context}.request_count", minimum=1) != request_count:
        raise InferenceReceiptError(f"{context}.request_count does not match the workload")
    elapsed = _number(row["elapsed_ms"], f"{context}.elapsed_ms", minimum=0.000000001)
    stages = _object(row["stages_ms"], f"{context}.stages_ms")
    _exact(stages, set(STAGES), f"{context}.stages_ms")
    stage_total = sum(_number(stages[stage], f"{context}.stages_ms.{stage}") for stage in STAGES)
    _same_float(elapsed, stage_total, f"{context}.elapsed_ms")
    capture_artifact_id = _identifier(
        row["capture_artifact_id"], f"{context}.capture_artifact_id"
    )
    artifact = artifacts.get(capture_artifact_id)
    if artifact is None or artifact["kind"] != "sample_capture":
        raise InferenceReceiptError(f"{context}.capture_artifact_id must reference a sample_capture")
    if row["status"] != "ok":
        raise InferenceReceiptError(f"{context}.status must be ok")
    return {
        "sample_id": sample_id,
        "arm": arm,
        "elapsed_ms": elapsed,
        "output_token_ids_sha256": _sha256(
            row["output_token_ids_sha256"], f"{context}.output_token_ids_sha256"
        ),
        "output_token_count": _integer(
            row["output_token_count"], f"{context}.output_token_count", minimum=1
        ),
        "quality_sha256": _sha256(row["quality_sha256"], f"{context}.quality_sha256"),
        "capture_artifact_id": capture_artifact_id,
    }


def _nearest_rank(values: Sequence[float], percentile: int) -> float:
    if not values:
        raise InferenceReceiptError("cannot calculate a percentile from no samples")
    if percentile not in {50, 95}:
        raise InferenceReceiptError("only p50 and p95 are supported")
    sorted_values = sorted(values)
    rank = max(1, math.ceil(percentile / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def _validate_samples(
    value: Any,
    *,
    workload_sha256: str,
    request_count: int,
    artifacts: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int, str, int, str]:
    if not isinstance(value, list):
        raise InferenceReceiptError("receipt.samples must be a list")
    if len(value) < 2 * MIN_TRIALS_PER_ARM or len(value) % len(ABBA):
        raise InferenceReceiptError(
            "receipt.samples must contain complete ABBA blocks with the minimum trials per arm"
        )
    samples = [
        _validate_sample(
            item,
            index=index,
            workload_sha256=workload_sha256,
            request_count=request_count,
            artifacts=artifacts,
        )
        for index, item in enumerate(value)
    ]
    sample_ids = [sample["sample_id"] for sample in samples]
    if len(sample_ids) != len(set(sample_ids)):
        raise InferenceReceiptError("receipt.samples has duplicate sample_id")
    arms = tuple(sample["arm"] for sample in samples)
    for offset in range(0, len(arms), len(ABBA)):
        if arms[offset : offset + len(ABBA)] != ABBA:
            raise InferenceReceiptError("receipt.samples must use fixed ABBA ordering")
    baseline_count = arms.count("baseline")
    candidate_count = arms.count("candidate")
    if baseline_count != candidate_count or baseline_count < MIN_TRIALS_PER_ARM:
        raise InferenceReceiptError("receipt.samples has insufficient or unbalanced trials")
    output_hashes = {sample["output_token_ids_sha256"] for sample in samples}
    output_counts = {sample["output_token_count"] for sample in samples}
    quality_hashes = {sample["quality_sha256"] for sample in samples}
    if len(output_hashes) != 1 or len(output_counts) != 1:
        raise InferenceReceiptError("receipt.samples are not exact-token stable across ABBA trials")
    if len(quality_hashes) != 1:
        raise InferenceReceiptError("receipt.samples do not share one quality result")
    return samples, baseline_count, next(iter(output_hashes)), next(iter(output_counts)), next(iter(quality_hashes))


def _validate_parity(
    value: Any,
    *,
    output_sha256: str,
    output_token_count: int,
    artifacts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    row = _object(value, "receipt.parity")
    _exact(
        row,
        {
            "policy",
            "status",
            "baseline_candidate_exact",
            "output_token_ids_sha256",
            "output_token_count",
            "parity_artifact_id",
        },
        "receipt.parity",
    )
    if row["policy"] != "exact_output_token_ids" or row["status"] != "passed":
        raise InferenceReceiptError("receipt.parity must pass exact_output_token_ids")
    if not _boolean(row["baseline_candidate_exact"], "receipt.parity.baseline_candidate_exact"):
        raise InferenceReceiptError("receipt.parity.baseline_candidate_exact must be true")
    if _sha256(row["output_token_ids_sha256"], "receipt.parity.output_token_ids_sha256") != output_sha256:
        raise InferenceReceiptError("receipt.parity output digest does not match samples")
    if _integer(row["output_token_count"], "receipt.parity.output_token_count", minimum=1) != output_token_count:
        raise InferenceReceiptError("receipt.parity output token count does not match samples")
    artifact_id = _identifier(row["parity_artifact_id"], "receipt.parity.parity_artifact_id")
    if artifact_id not in artifacts or artifacts[artifact_id]["kind"] != "parity_audit":
        raise InferenceReceiptError("receipt.parity.parity_artifact_id must reference a parity_audit")
    return copy.deepcopy(row)


def _validate_quality(
    value: Any,
    *,
    quality_sha256: str,
    artifacts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    row = _object(value, "receipt.quality")
    _exact(row, {"policy", "status", "summary_sha256", "quality_artifact_id"}, "receipt.quality")
    if row["policy"] != "exact_output_token_ids" or row["status"] != "passed":
        raise InferenceReceiptError("receipt.quality must pass exact_output_token_ids")
    if _sha256(row["summary_sha256"], "receipt.quality.summary_sha256") != quality_sha256:
        raise InferenceReceiptError("receipt.quality.summary_sha256 does not match samples")
    artifact_id = _identifier(row["quality_artifact_id"], "receipt.quality.quality_artifact_id")
    if artifact_id not in artifacts or artifacts[artifact_id]["kind"] != "quality_audit":
        raise InferenceReceiptError("receipt.quality.quality_artifact_id must reference a quality_audit")
    return copy.deepcopy(row)


def _validate_statistics(
    value: Any,
    *,
    samples: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    row = _object(value, "receipt.statistics")
    _exact(
        row,
        {"unit", "baseline", "candidate", "p50_multiplier", "p95_multiplier"},
        "receipt.statistics",
    )
    if row["unit"] != "ms/request":
        raise InferenceReceiptError("receipt.statistics.unit must be ms/request")
    parsed: dict[str, tuple[float, float]] = {}
    for arm in ("baseline", "candidate"):
        summary = _object(row[arm], f"receipt.statistics.{arm}")
        _exact(summary, {"p50_ms", "p95_ms"}, f"receipt.statistics.{arm}")
        declared_p50 = _number(summary["p50_ms"], f"receipt.statistics.{arm}.p50_ms", minimum=0.000000001)
        declared_p95 = _number(summary["p95_ms"], f"receipt.statistics.{arm}.p95_ms", minimum=0.000000001)
        values = [float(sample["elapsed_ms"]) for sample in samples if sample["arm"] == arm]
        expected_p50 = _nearest_rank(values, 50)
        expected_p95 = _nearest_rank(values, 95)
        _same_float(declared_p50, expected_p50, f"receipt.statistics.{arm}.p50_ms")
        _same_float(declared_p95, expected_p95, f"receipt.statistics.{arm}.p95_ms")
        parsed[arm] = (expected_p50, expected_p95)
    expected_p50_multiplier = parsed["baseline"][0] / parsed["candidate"][0]
    expected_p95_multiplier = parsed["baseline"][1] / parsed["candidate"][1]
    _same_float(
        _number(row["p50_multiplier"], "receipt.statistics.p50_multiplier", minimum=0.000000001),
        expected_p50_multiplier,
        "receipt.statistics.p50_multiplier",
    )
    _same_float(
        _number(row["p95_multiplier"], "receipt.statistics.p95_multiplier", minimum=0.000000001),
        expected_p95_multiplier,
        "receipt.statistics.p95_multiplier",
    )
    return copy.deepcopy(row)


def _validate_artifact_references(
    artifacts: Mapping[str, Mapping[str, Any]],
    samples: Sequence[Mapping[str, Any]],
    parity: Mapping[str, Any],
    quality: Mapping[str, Any],
) -> None:
    referenced = {sample["capture_artifact_id"] for sample in samples}
    referenced.add(parity["parity_artifact_id"])
    referenced.add(quality["quality_artifact_id"])
    if referenced != set(artifacts):
        raise InferenceReceiptError("receipt.artifacts contains an unreferenced or missing immutable artifact")


def validate_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a receipt's complete internal binding without reading artifacts."""

    row = _object(value, "receipt")
    _exact(
        row,
        {
            "schema_version",
            "record_kind",
            "receipt_id",
            "claim_scope",
            "lane",
            "scorecard_binding",
            "workload",
            "workload_sha256",
            "runtime",
            "comparison",
            "reuse",
            "fallback",
            "authorization",
            "attestation",
            "artifacts",
            "samples",
            "parity",
            "quality",
            "statistics",
            "receipt_sha256",
        },
        "receipt",
    )
    if type(row["schema_version"]) is not int or row["schema_version"] != SCHEMA_VERSION:
        raise InferenceReceiptError(f"receipt.schema_version must be {SCHEMA_VERSION}")
    if row["record_kind"] != RECORD_KIND:
        raise InferenceReceiptError(f"receipt.record_kind must be {RECORD_KIND}")
    _identifier(row["receipt_id"], "receipt.receipt_id")
    if row["claim_scope"] != "customer_visible_inference_request_turnaround":
        raise InferenceReceiptError("receipt.claim_scope must be customer_visible_inference_request_turnaround")
    lane = row["lane"]
    if lane not in LANES:
        raise InferenceReceiptError("receipt.lane must be a distinct supported inference lane")
    workload = _validate_workload(row["workload"])
    workload_sha256 = _sha256(row["workload_sha256"], "receipt.workload_sha256")
    if workload_sha256 != sha256_json(workload):
        raise InferenceReceiptError("receipt.workload_sha256 does not bind receipt.workload")
    _validate_scorecard_binding(row["scorecard_binding"], lane)
    _validate_runtime(row["runtime"])
    _validate_comparison(row["comparison"])
    _validate_reuse(row["reuse"], lane)
    _validate_fallback(row["fallback"])
    _validate_authorization(row["authorization"])
    _validate_attestation(row["attestation"])
    artifacts = _validate_artifacts(row["artifacts"])
    samples, trial_count, output_sha, output_count, quality_sha = _validate_samples(
        row["samples"],
        workload_sha256=workload_sha256,
        request_count=int(workload["request_count"]),
        artifacts=artifacts,
    )
    parity = _validate_parity(
        row["parity"],
        output_sha256=output_sha,
        output_token_count=output_count,
        artifacts=artifacts,
    )
    quality = _validate_quality(row["quality"], quality_sha256=quality_sha, artifacts=artifacts)
    _validate_artifact_references(artifacts, samples, parity, quality)
    _validate_statistics(row["statistics"], samples=samples)
    declared_receipt_sha = _sha256(row["receipt_sha256"], "receipt.receipt_sha256")
    if declared_receipt_sha != receipt_sha256(row):
        raise InferenceReceiptError("receipt.receipt_sha256 does not bind the full receipt")
    result = copy.deepcopy(row)
    result["workload"] = workload
    return result


def _read_bound_artifact(root: Path, relative_path: str) -> bytes:
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise InferenceReceiptError(f"artifact root cannot be resolved: {exc}") from exc
    candidate = resolved_root / relative_path
    try:
        resolved_parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise InferenceReceiptError(f"artifact parent cannot be resolved for {relative_path}: {exc}") from exc
    if resolved_parent != resolved_root and resolved_root not in resolved_parent.parents:
        raise InferenceReceiptError(f"artifact path escapes its root: {relative_path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise InferenceReceiptError(f"cannot securely open artifact {relative_path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise InferenceReceiptError(f"artifact {relative_path} is not a regular file")
        if before.st_size < 1 or before.st_size > MAX_ARTIFACT_BYTES:
            raise InferenceReceiptError(f"artifact {relative_path} has an invalid size")
        chunks: list[bytes] = []
        remaining = MAX_ARTIFACT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1 << 20, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if len(data) != before.st_size or len(data) > MAX_ARTIFACT_BYTES:
        raise InferenceReceiptError(f"artifact {relative_path} changed or exceeds the size limit")
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise InferenceReceiptError(f"artifact {relative_path} changed while being read")
    return data


def verify_artifact_bindings(receipt: Mapping[str, Any], artifact_root: Path | str) -> None:
    """Hash every declared external artifact under one non-escaping root."""

    validated = validate_receipt(receipt)
    root = Path(artifact_root)
    for raw in validated["artifacts"]:
        data = _read_bound_artifact(root, raw["path"])
        if sha256_bytes(data) != raw["sha256"]:
            raise InferenceReceiptError(f"artifact SHA-256 mismatch: {raw['artifact_id']}")


def load_receipt_path(
    path: Path | str,
    *,
    artifact_root: Path | str | None = None,
    verify_artifacts: bool = False,
) -> tuple[dict[str, Any], str]:
    """Load a receipt and, when requested, verify all retained byte artifacts."""

    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise InferenceReceiptError(f"cannot read receipt {source}: {exc}") from exc
    receipt = validate_receipt(parse_json_bytes(raw, context=str(source)))
    if verify_artifacts:
        if artifact_root is None:
            raise InferenceReceiptError("artifact_root is required when verifying artifact bindings")
        verify_artifact_bindings(receipt, artifact_root)
    return receipt, sha256_bytes(raw)


def evaluate_receipt(
    receipt: Mapping[str, Any],
    *,
    artifacts_verified: bool = False,
) -> dict[str, Any]:
    """Return derived, per-lane facts without inventing a cross-lane result."""

    validated = validate_receipt(receipt)
    statistics = validated["statistics"]
    target = float(validated["comparison"]["target_multiplier"])
    p50_multiplier = float(statistics["p50_multiplier"])
    p95_multiplier = float(statistics["p95_multiplier"])
    trials = sum(sample["arm"] == "baseline" for sample in validated["samples"])
    authorization_ready = all(validated["authorization"].values())
    coverage = validated["reuse"]["coverage"]
    return {
        "schema_version": SCHEMA_VERSION,
        "record_kind": "cx_inference_50x_lane_evaluation",
        "receipt_id": validated["receipt_id"],
        "receipt_sha256": validated["receipt_sha256"],
        "lane": validated["lane"],
        "scorecard_lane_id": validated["scorecard_binding"]["lane_id"],
        "comparison_group": validated["scorecard_binding"]["comparison_group"],
        "workload_sha256": validated["workload_sha256"],
        "trials_per_arm": trials,
        "minimum_trials_per_arm": MIN_TRIALS_PER_ARM,
        "p50_multiplier": p50_multiplier,
        "p95_multiplier": p95_multiplier,
        "target_multiplier": target,
        "p50_target_met": p50_multiplier >= target,
        "p95_target_met": p95_multiplier >= target,
        "exact_token_parity": True,
        "quality_passed": True,
        "fallback_validated": True,
        "coverage": copy.deepcopy(coverage),
        "artifacts_verified": artifacts_verified,
        "authorization_ready": authorization_ready,
        "production_promotion_eligible": (
            artifacts_verified
            and authorization_ready
            and p50_multiplier >= target
            and p95_multiplier >= target
        ),
        "claim_scope": (
            "One declared lane only; no cache, prefix, batching, or speculative-decode "
            "multipliers are combined."
        ),
    }


def scorecard_projection(receipt: Mapping[str, Any], *, receipt_file_sha256: str) -> dict[str, Any]:
    """Project a validated receipt into the exact fields a scorecard must bind."""

    validated = validate_receipt(receipt)
    raw_sha = _sha256(receipt_file_sha256, "receipt_file_sha256")
    statistics = validated["statistics"]
    coverage = validated["reuse"]["coverage"]
    projected_coverage = None
    if coverage is not None:
        projected_coverage = {
            "required_hit_rate": coverage["required_eligible_hit_rate"],
            "observed_hit_rate": coverage["eligible_hit_rate"],
            "tail_multiplier": statistics["p95_multiplier"],
            "tail_statistic": "p95",
        }
    trials = sum(sample["arm"] == "baseline" for sample in validated["samples"])
    return {
        "lane_id": validated["scorecard_binding"]["lane_id"],
        "display_name": validated["scorecard_binding"]["display_name"],
        "comparison_group": validated["scorecard_binding"]["comparison_group"],
        "modality": "inference",
        "metric_kind": "inference_endpoint",
        "claim_axis": "inference_request_turnaround",
        "measurement": {
            "direction": "lower_is_better",
            "unit": "ms/request",
            "statistic": "median",
            "baseline_value": statistics["baseline"]["p50_ms"],
            "candidate_value": statistics["candidate"]["p50_ms"],
            "same_logical_work": True,
            "timing_scope": "integrated_wall",
            "baseline_source": "measured",
            "evidence_class": validated["attestation"]["evidence_class"],
            "trials": trials,
        },
        "quality_gate": "exact",
        "authorization": copy.deepcopy(validated["authorization"]),
        "evidence": {
            "receipt_sha256": raw_sha,
            "workload_sha256": validated["workload_sha256"],
        },
        "coverage": projected_coverage,
        "p50_multiplier": statistics["p50_multiplier"],
        "p95_multiplier": statistics["p95_multiplier"],
        "target_multiplier": validated["comparison"]["target_multiplier"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--verify-artifacts", action="store_true")
    parser.add_argument("--require-p50-target", action="store_true")
    parser.add_argument("--require-tail-target", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt, raw_sha = load_receipt_path(
            args.receipt,
            artifact_root=args.artifact_root,
            verify_artifacts=args.verify_artifacts,
        )
        result = evaluate_receipt(receipt, artifacts_verified=args.verify_artifacts)
        result["receipt_file_sha256"] = raw_sha
        output = json.dumps(result, sort_keys=True, indent=2, allow_nan=False) + "\n"
        if args.output is None:
            sys.stdout.write(output)
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output, encoding="utf-8")
        if args.require_tail_target and not (
            result["p50_target_met"] and result["p95_target_met"]
        ):
            return 3
        if args.require_p50_target and not result["p50_target_met"]:
            return 3
        return 0
    except InferenceReceiptError as exc:
        print(f"cx-inference-receipt-v1: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
