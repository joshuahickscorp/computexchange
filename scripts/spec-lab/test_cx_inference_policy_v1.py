#!/usr/bin/env python3
"""Tests for the CX-owned, fail-closed inference routing policy."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import sys
import unittest


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cx_inference_policy_v1 as policy  # noqa: E402


def digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def request(*, prefix: bool = False, intent: str = policy.INTENT_FINAL) -> dict[str, object]:
    value: dict[str, object] = {
        "contract": policy.REQUEST_CONTRACT,
        "request_identity_sha256": "0" * 64,
        "tenant_scope_sha256": digest("tenant-a"),
        "model_id": "mlx-community/Llama-3.2-1B-Instruct-4bit",
        "model_revision": "0" * 40,
        "tokenizer_sha256": digest("tokenizer"),
        "runtime_sha256": digest("runtime"),
        "sampling_contract_sha256": digest("greedy"),
        "input_token_ids_sha256": digest("input-token-ids"),
        "shared_prefix_token_ids_sha256": digest("prefix-token-ids") if prefix else None,
        "shared_prefix_token_count": 128 if prefix else 0,
        "max_output_tokens": 64,
        "concurrency": 1,
        "intent": intent,
        "response_reuse_authorized": True,
        "prefix_reuse_authorized": True,
    }
    value["request_identity_sha256"] = policy.request_identity_sha256(value)
    return value


def inactive_speculator() -> dict[str, object]:
    return {
        "status": "unavailable",
        "receipt_sha256": None,
        "runtime_sha256": None,
        "model_revision": None,
        "tokenizer_sha256": None,
        "proposer_config_sha256": None,
        "verified_lane": None,
        "prefix_cache_allowed": False,
        "min_concurrency": 1,
        "max_concurrency": 1,
        "exact_token_parity": False,
        "repeat_stable": False,
        "production_authorized": False,
        "direct_fallback_available": False,
        "confidence": 0.0,
        "minimum_confidence": 0.5,
        "p50_speedup_x": 1.0,
    }


def evidence() -> dict[str, object]:
    return {
        "exact_cache": {
            "status": "miss",
            "request_identity_sha256": None,
            "tenant_scope_sha256": None,
            "artifact_sha256": None,
            "output_token_ids_sha256": None,
            "target_output_token_ids_sha256": None,
            "artifact_verified": False,
            "exact_token_parity": False,
            "production_authorized": False,
            "direct_fallback_available": True,
        },
        "prefix_cache": {
            "status": "miss",
            "tenant_scope_sha256": None,
            "model_revision": None,
            "tokenizer_sha256": None,
            "runtime_sha256": None,
            "prefix_token_ids_sha256": None,
            "prefix_token_count": 0,
            "cache_entry_sha256": None,
            "cache_history_sha256": None,
            "integrity_verified": False,
            "production_authorized": False,
            "direct_fallback_available": True,
        },
        "speculators": {kind: inactive_speculator() for kind in policy.SPECULATORS},
    }


def current_exact(req: dict[str, object]) -> dict[str, object]:
    value = evidence()
    value["exact_cache"] = {
        "status": "current",
        "request_identity_sha256": req["request_identity_sha256"],
        "tenant_scope_sha256": req["tenant_scope_sha256"],
        "artifact_sha256": digest("artifact"),
        "output_token_ids_sha256": digest("output-tokens"),
        "target_output_token_ids_sha256": digest("output-tokens"),
        "artifact_verified": True,
        "exact_token_parity": True,
        "production_authorized": True,
        "direct_fallback_available": True,
    }
    return value


def current_prefix(req: dict[str, object]) -> dict[str, object]:
    value = evidence()
    value["prefix_cache"] = {
        "status": "current",
        "tenant_scope_sha256": req["tenant_scope_sha256"],
        "model_revision": req["model_revision"],
        "tokenizer_sha256": req["tokenizer_sha256"],
        "runtime_sha256": req["runtime_sha256"],
        "prefix_token_ids_sha256": req["shared_prefix_token_ids_sha256"],
        "prefix_token_count": req["shared_prefix_token_count"],
        "cache_entry_sha256": digest("kv-entry"),
        "cache_history_sha256": digest("cache-history"),
        "integrity_verified": True,
        "production_authorized": True,
        "direct_fallback_available": True,
    }
    return value


def qualifying_speculator(
    req: dict[str, object],
    *,
    lane: str,
    speedup: float,
    prefix_cache_allowed: bool = True,
) -> dict[str, object]:
    return {
        "status": "current",
        "receipt_sha256": digest(f"{lane}-{speedup}"),
        "runtime_sha256": req["runtime_sha256"],
        "model_revision": req["model_revision"],
        "tokenizer_sha256": req["tokenizer_sha256"],
        "proposer_config_sha256": digest("proposer-config"),
        "verified_lane": lane,
        "prefix_cache_allowed": prefix_cache_allowed,
        "min_concurrency": 1,
        "max_concurrency": 16,
        "exact_token_parity": True,
        "repeat_stable": True,
        "production_authorized": True,
        "direct_fallback_available": True,
        "confidence": 0.99,
        "minimum_confidence": 0.95,
        "p50_speedup_x": speedup,
    }


class InferencePolicyRoutingTests(unittest.TestCase):
    def test_verified_exact_cache_wins_and_is_bound_to_full_identity(self):
        req = request()
        result = policy.decide_inference_route(req, current_exact(req))
        self.assertEqual(result["delivered_route"], policy.ROUTE_EXACT_CACHE)
        self.assertEqual(result["cache_scope"], "exact_request")
        self.assertEqual(result["execution_path"], ["exact_response_cache"])
        self.assertIsNone(result["fallback_reason"])

    def test_exact_cache_cannot_cross_tenant_or_request_identity(self):
        req = request()
        value = current_exact(req)
        value["exact_cache"]["tenant_scope_sha256"] = digest("other-tenant")
        result = policy.decide_inference_route(req, value)
        self.assertEqual(result["delivered_route"], policy.ROUTE_DIRECT_DECODE)
        self.assertNotEqual(result["cache_scope"], "exact_request")

    def test_exact_cache_requires_request_authorization(self):
        req = request()
        req["response_reuse_authorized"] = False
        req["request_identity_sha256"] = policy.request_identity_sha256(req)
        result = policy.decide_inference_route(req, current_exact(req))
        self.assertEqual(result["delivered_route"], policy.ROUTE_DIRECT_DECODE)

    def test_exact_cache_requires_exact_target_token_parity(self):
        req = request()
        value = current_exact(req)
        value["exact_cache"]["target_output_token_ids_sha256"] = digest("different-target")
        result = policy.decide_inference_route(req, value)
        self.assertEqual(result["delivered_route"], policy.ROUTE_DIRECT_DECODE)

    def test_valid_prefix_hit_uses_direct_target_when_no_speculator_is_safe(self):
        req = request(prefix=True)
        result = policy.decide_inference_route(req, current_prefix(req))
        self.assertEqual(result["delivered_route"], policy.ROUTE_PREFIX_CACHE_DIRECT)
        self.assertEqual(result["execution_path"], ["prefix_kv_cache", "direct_decode"])
        self.assertEqual(result["cache_scope"], "shared_prefix")

    def test_c1_ngram_topology_falls_back_to_direct_target_even_without_prefix_cache(self):
        req = request(prefix=False)
        value = evidence()
        value["speculators"]["ngram"] = qualifying_speculator(
            req,
            lane=policy.FRESH_LANE,
            speedup=2.0,
        )
        result = policy.decide_inference_route(req, value)
        self.assertEqual(result["delivered_route"], policy.ROUTE_DIRECT_DECODE)
        self.assertIsNone(result["selected_speculator"])

    def test_c1_prefix_ngram_topology_falls_back_to_direct_target(self):
        req = request(prefix=True)
        value = current_prefix(req)
        value["speculators"]["ngram"] = qualifying_speculator(
            req,
            lane=policy.PREFIX_LANE,
            speedup=2.0,
            prefix_cache_allowed=False,
        )
        result = policy.decide_inference_route(req, value)
        self.assertEqual(result["delivered_route"], policy.ROUTE_PREFIX_CACHE_DIRECT)
        self.assertIsNone(result["selected_speculator"])

    def test_prefix_route_selects_fastest_individually_qualified_speculator(self):
        req = request(prefix=True)
        value = current_prefix(req)
        value["speculators"]["ngram"] = qualifying_speculator(
            req, lane=policy.PREFIX_LANE, speedup=1.7
        )
        value["speculators"]["draft_model"] = qualifying_speculator(
            req, lane=policy.PREFIX_LANE, speedup=2.4
        )
        result = policy.decide_inference_route(req, value)
        self.assertEqual(result["delivered_route"], policy.ROUTE_PREFIX_CACHE_DRAFT_MODEL)
        self.assertEqual(result["selected_speculator"], "draft_model")
        self.assertEqual(result["cache_scope"], "shared_prefix")

    def test_fresh_speculator_requires_fresh_lane_and_never_claims_prefix_reuse(self):
        req = request()
        value = evidence()
        value["speculators"]["mtp"] = qualifying_speculator(
            req, lane=policy.FRESH_LANE, speedup=2.2
        )
        result = policy.decide_inference_route(req, value)
        self.assertEqual(result["delivered_route"], policy.ROUTE_MTP)
        self.assertEqual(result["cache_scope"], "none")
        self.assertEqual(result["execution_path"], ["mtp"])

    def test_fresh_receipt_cannot_authorize_a_prefix_route(self):
        req = request(prefix=True)
        value = current_prefix(req)
        value["speculators"]["mtp"] = qualifying_speculator(
            req, lane=policy.FRESH_LANE, speedup=2.2
        )
        result = policy.decide_inference_route(req, value)
        self.assertEqual(result["delivered_route"], policy.ROUTE_PREFIX_CACHE_DIRECT)

    def test_confidence_parity_and_direct_fallback_are_all_required(self):
        req = request()
        for field, value in (
            ("confidence", 0.9),
            ("exact_token_parity", False),
            ("repeat_stable", False),
            ("direct_fallback_available", False),
        ):
            with self.subTest(field=field):
                evidence_value = evidence()
                candidate = qualifying_speculator(req, lane=policy.FRESH_LANE, speedup=2.0)
                candidate[field] = value
                evidence_value["speculators"]["ngram"] = candidate
                result = policy.decide_inference_route(req, evidence_value)
                self.assertEqual(result["delivered_route"], policy.ROUTE_DIRECT_DECODE)

    def test_runtime_binding_and_concurrency_range_are_required(self):
        req = request()
        for field, value in (("runtime_sha256", digest("wrong-runtime")), ("max_concurrency", 0)):
            with self.subTest(field=field):
                evidence_value = evidence()
                candidate = qualifying_speculator(req, lane=policy.FRESH_LANE, speedup=2.0)
                if field == "max_concurrency":
                    # Keep a valid schema but make this request fall outside it.
                    value = 0
                    candidate["min_concurrency"] = 2
                    candidate["max_concurrency"] = 16
                else:
                    candidate[field] = value
                evidence_value["speculators"]["ngram"] = candidate
                result = policy.decide_inference_route(req, evidence_value)
                self.assertEqual(result["delivered_route"], policy.ROUTE_DIRECT_DECODE)

    def test_decision_is_deterministic_and_non_mutating(self):
        req = request(prefix=True)
        value = current_prefix(req)
        value["speculators"]["ngram"] = qualifying_speculator(
            req, lane=policy.PREFIX_LANE, speedup=2.0
        )
        original_request = copy.deepcopy(req)
        original_evidence = copy.deepcopy(value)
        self.assertEqual(
            policy.decide_inference_route(req, value),
            policy.decide_inference_route(req, value),
        )
        self.assertEqual(req, original_request)
        self.assertEqual(value, original_evidence)


class InferencePolicyValidationTests(unittest.TestCase):
    def test_valid_input_passes_strict_validator(self):
        req = request()
        value = evidence()
        self.assertIsNone(policy.validate_policy_input(req, value))

    def test_invalid_request_is_rejected_without_an_executable_route(self):
        req = request()
        req["concurrency"] = 2  # identity is now stale.
        result = policy.decide_inference_route(req, evidence())
        self.assertEqual(result["attempted_route"], policy.ROUTE_REJECTED_NO_EXECUTION)
        self.assertEqual(result["delivered_route"], policy.ROUTE_REJECTED_NO_EXECUTION)
        self.assertEqual(result["execution_path"], [])

    def test_unknown_evidence_field_falls_back_without_executing_cache_or_speculation(self):
        req = request()
        value = evidence()
        value["prefix_cache"]["unbound_fast_path"] = True
        result = policy.decide_inference_route(req, value)
        self.assertEqual(result["delivered_route"], policy.ROUTE_DIRECT_DECODE)
        self.assertEqual(result["fallback_reason"], "invalid_policy_evidence")

    def test_shared_prefix_digest_and_count_are_indivisible(self):
        req = request(prefix=True)
        req["shared_prefix_token_count"] = 0
        req["request_identity_sha256"] = policy.request_identity_sha256(req)
        with self.assertRaises(policy.PolicyInputError):
            policy.validate_policy_input(req, evidence())

    def test_minimum_confidence_cannot_be_below_half(self):
        req = request()
        value = evidence()
        candidate = qualifying_speculator(req, lane=policy.FRESH_LANE, speedup=2.0)
        candidate["minimum_confidence"] = 0.49
        value["speculators"]["ngram"] = candidate
        with self.assertRaises(policy.PolicyInputError):
            policy.validate_policy_input(req, value)


if __name__ == "__main__":
    unittest.main()
