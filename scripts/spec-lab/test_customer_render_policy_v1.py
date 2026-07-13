#!/usr/bin/env python3
"""Unit tests for the pure, lab-only customer render policy."""

from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import customer_render_policy_v1 as policy  # noqa: E402


def request(intent: str = policy.INTENT_EXPERIMENTAL_PREVIEW) -> dict[str, object]:
    return {"intent": intent, "request_identity": "request-sha256-a"}


def evidence() -> dict[str, object]:
    return {
        "exact_cache": {
            "evidence_status": "unavailable",
            "artifact_request_identity": None,
            "source_eligibility": {
                "experimental_preview": False,
                "production_ready": False,
                "artifact_verified": False,
                "billing_eligible": False,
            },
        },
        "spatial75": {
            "evidence_status": "unavailable",
            "request_identity": None,
            "fresh_two_render_gate_pass": False,
        },
    }


def current_exact(*, preview: bool = True, final: bool = False) -> dict[str, object]:
    value = evidence()
    value["exact_cache"] = {
        "evidence_status": "current",
        "artifact_request_identity": "request-sha256-a",
        "source_eligibility": {
            "experimental_preview": preview,
            "production_ready": final,
            "artifact_verified": final,
            "billing_eligible": final,
        },
    }
    return value


def current_spatial() -> dict[str, object]:
    value = evidence()
    value["spatial75"] = {
        "evidence_status": "current",
        "request_identity": "request-sha256-a",
        "fresh_two_render_gate_pass": True,
    }
    return value


class CustomerRenderPolicyRoutingTest(unittest.TestCase):
    def test_exact_cache_is_first_for_experimental_preview(self):
        value = current_exact()
        # An unused, lower-priority route cannot overturn an exact hit.
        value["spatial75"] = {
            "evidence_status": "error",
            "request_identity": "request-sha256-a",
            "fresh_two_render_gate_pass": False,
        }
        result = policy.decide_customer_render(request(), value)
        self.assertEqual(result["attempted_route"], "exact_cache")
        self.assertEqual(result["delivered_route"], "exact_cache")
        self.assertIsNone(result["fallback_reason"])
        self.assertEqual(result["billability_ceiling"], "non_billable")
        self.assertEqual(
            result["route_order"],
            ["exact_cache", "spatial75", "basic_cycles"],
        )

    def test_exact_cache_requires_identity_match(self):
        value = current_exact()
        value["exact_cache"]["artifact_request_identity"] = "different-request"
        value["spatial75"] = current_spatial()["spatial75"]
        result = policy.decide_customer_render(request(), value)
        self.assertEqual(result["delivered_route"], "spatial75")

    def test_exact_cache_requires_preview_source_eligibility(self):
        value = current_exact(preview=False)
        value["spatial75"] = current_spatial()["spatial75"]
        result = policy.decide_customer_render(request(), value)
        self.assertEqual(result["delivered_route"], "spatial75")

    def test_final_eligible_cache_can_satisfy_preview_without_bill_escalation(self):
        value = current_exact(preview=False, final=True)
        result = policy.decide_customer_render(request(), value)
        self.assertEqual(result["delivered_route"], "exact_cache")
        self.assertEqual(result["billability_ceiling"], "non_billable")

    def test_stale_exact_cache_can_only_be_a_cache_miss(self):
        value = current_exact()
        value["exact_cache"]["evidence_status"] = "stale"
        value["spatial75"] = current_spatial()["spatial75"]
        result = policy.decide_customer_render(request(), value)
        self.assertEqual(result["delivered_route"], "spatial75")

    def test_spatial75_requires_current_evidence_and_fresh_gate(self):
        result = policy.decide_customer_render(request(), current_spatial())
        self.assertEqual(result["attempted_route"], "spatial75")
        self.assertEqual(result["delivered_route"], "spatial75")
        self.assertEqual(result["billability_ceiling"], "non_billable")

    def test_spatial75_requires_exact_request_binding(self):
        value = current_spatial()
        value["spatial75"]["request_identity"] = "other-request"
        result = policy.decide_customer_render(request(), value)
        self.assertEqual(result["delivered_route"], "basic_cycles")
        self.assertEqual(result["fallback_reason"], "spatial75_identity_mismatch")

    def test_spatial75_gate_failure_falls_back(self):
        value = current_spatial()
        value["spatial75"]["fresh_two_render_gate_pass"] = False
        result = policy.decide_customer_render(request(), value)
        self.assertEqual(result["attempted_route"], "spatial75")
        self.assertEqual(result["delivered_route"], "basic_cycles")
        self.assertEqual(result["fallback_reason"], "spatial75_gate_not_passed")
        self.assertEqual(result["billability_ceiling"], "non_billable")

    def test_spatial75_stale_or_unavailable_falls_back(self):
        for status in ("stale", "unavailable"):
            with self.subTest(status=status):
                value = current_spatial()
                value["spatial75"]["evidence_status"] = status
                result = policy.decide_customer_render(request(), value)
                self.assertEqual(result["delivered_route"], "basic_cycles")
                self.assertEqual(
                    result["fallback_reason"],
                    "spatial75_evidence_not_current",
                )

    def test_exact_cache_fatal_statuses_stop_experimental_routing(self):
        for status in sorted(policy.FATAL_EVIDENCE_STATUSES):
            with self.subTest(status=status):
                value = current_spatial()
                value["exact_cache"]["evidence_status"] = status
                result = policy.decide_customer_render(request(), value)
                self.assertEqual(result["attempted_route"], "exact_cache")
                self.assertEqual(result["delivered_route"], "basic_cycles")
                self.assertEqual(result["fallback_reason"], f"exact_cache_{status}")

    def test_spatial75_fatal_statuses_stop_experimental_routing(self):
        for status in sorted(policy.FATAL_EVIDENCE_STATUSES):
            with self.subTest(status=status):
                value = evidence()
                value["spatial75"] = {
                    "evidence_status": status,
                    "request_identity": "request-sha256-a",
                    "fresh_two_render_gate_pass": False,
                }
                result = policy.decide_customer_render(request(), value)
                self.assertEqual(result["attempted_route"], "spatial75")
                self.assertEqual(result["delivered_route"], "basic_cycles")
                self.assertEqual(result["fallback_reason"], f"spatial75_{status}")

    def test_final_selects_only_fully_eligible_exact_artifact(self):
        result = policy.decide_customer_render(
            request(policy.INTENT_FINAL),
            current_exact(final=True),
        )
        self.assertEqual(result["delivered_route"], "exact_cache")
        self.assertEqual(
            result["billability_ceiling"],
            "standard_customer_render",
        )
        self.assertEqual(result["route_order"], ["exact_cache", "basic_cycles"])

    def test_final_requires_every_eligibility_flag(self):
        for key in ("production_ready", "artifact_verified", "billing_eligible"):
            with self.subTest(key=key):
                value = current_exact(final=True)
                value["exact_cache"]["source_eligibility"][key] = False
                result = policy.decide_customer_render(
                    request(policy.INTENT_FINAL),
                    value,
                )
                self.assertEqual(result["delivered_route"], "basic_cycles")
                expected_reason = (
                    "exact_cache_not_final_eligible"
                    if key == "billing_eligible"
                    else "invalid_policy_input"
                )
                self.assertEqual(result["fallback_reason"], expected_reason)

    def test_preview_eligibility_alone_never_satisfies_final(self):
        result = policy.decide_customer_render(
            request(policy.INTENT_FINAL),
            current_exact(preview=True, final=False),
        )
        self.assertEqual(result["delivered_route"], "basic_cycles")

    def test_final_never_selects_spatial75(self):
        result = policy.decide_customer_render(
            request(policy.INTENT_FINAL),
            current_spatial(),
        )
        self.assertEqual(result["attempted_route"], "exact_cache")
        self.assertEqual(result["delivered_route"], "basic_cycles")
        self.assertNotIn("spatial75", result["route_order"])

    def test_final_exact_identity_mismatch_falls_back(self):
        value = current_exact(final=True)
        value["exact_cache"]["artifact_request_identity"] = "other"
        result = policy.decide_customer_render(request(policy.INTENT_FINAL), value)
        self.assertEqual(result["delivered_route"], "basic_cycles")
        self.assertEqual(result["fallback_reason"], "exact_cache_identity_mismatch")

    def test_audit_only_routes_are_structurally_unselectable(self):
        result = policy.decide_customer_render(request(), current_spatial())
        self.assertEqual(
            result["audit_only_routes"],
            ["one_render_upper_bound", "temporal_prediction"],
        )
        self.assertNotIn(result["delivered_route"], result["audit_only_routes"])

    def test_decision_is_deterministic_and_does_not_mutate_inputs(self):
        req = request()
        ev = current_exact()
        original_req = copy.deepcopy(req)
        original_ev = copy.deepcopy(ev)
        first = policy.decide_customer_render(req, ev)
        second = policy.decide_customer_render(req, ev)
        self.assertEqual(first, second)
        self.assertEqual(req, original_req)
        self.assertEqual(ev, original_ev)


class CustomerRenderPolicyValidationTest(unittest.TestCase):
    def assertInvalid(self, req: object, ev: object) -> None:  # noqa: N802
        with self.assertRaises(policy.PolicyInputError):
            policy.validate_policy_input(req, ev)
        result = policy.decide_customer_render(req, ev)
        try:
            expected_intent, _ = policy._validate_request(req)
        except policy.PolicyInputError:
            self.assertIsNone(result["request_intent"])
            self.assertEqual(
                result["attempted_route"], policy.ROUTE_REJECTED_NO_EXECUTION
            )
            self.assertEqual(
                result["delivered_route"], policy.ROUTE_REJECTED_NO_EXECUTION
            )
            self.assertEqual(result["fallback_reason"], "invalid_request_contract")
            self.assertEqual(
                result["route_order"], [policy.ROUTE_REJECTED_NO_EXECUTION]
            )
            self.assertNotIn(policy.ROUTE_BASIC_CYCLES, result["route_order"])
            self.assertEqual(
                result["billability_ceiling"], policy.BILLABILITY_NON_BILLABLE
            )
            return

        self.assertEqual(result["attempted_route"], policy.ROUTE_BASIC_CYCLES)
        self.assertEqual(result["delivered_route"], policy.ROUTE_BASIC_CYCLES)
        self.assertEqual(result["fallback_reason"], "invalid_policy_input")
        expected_billability = (
            "standard_customer_render"
            if expected_intent == policy.INTENT_FINAL
            else "non_billable"
        )
        self.assertEqual(result["billability_ceiling"], expected_billability)
        self.assertEqual(result["request_intent"], expected_intent)

    def test_malformed_request_contract_is_rejected_without_executable_route(self):
        malformed_requests: tuple[object, ...] = (
            None,
            "request",
            {},
            {"intent": "preview", "request_identity": "request-sha256-a"},
            {
                "intent": policy.INTENT_FINAL,
                "request_identity": "request-sha256-a",
                "unknown": True,
            },
        )
        executable_routes = {
            policy.ROUTE_BASIC_CYCLES,
            policy.ROUTE_EXACT_CACHE,
            policy.ROUTE_SPATIAL75,
        }
        for malformed in malformed_requests:
            with self.subTest(request=malformed):
                result = policy.decide_customer_render(malformed, evidence())
                self.assertEqual(
                    result["delivered_route"], policy.ROUTE_REJECTED_NO_EXECUTION
                )
                self.assertEqual(
                    result["attempted_route"], policy.ROUTE_REJECTED_NO_EXECUTION
                )
                self.assertEqual(result["fallback_reason"], "invalid_request_contract")
                self.assertTrue(
                    executable_routes.isdisjoint(result["route_order"]),
                    result,
                )
                self.assertEqual(
                    result["billability_ceiling"], policy.BILLABILITY_NON_BILLABLE
                )

    def test_valid_input_passes_strict_validator(self):
        self.assertIsNone(policy.validate_policy_input(request(), evidence()))

    def test_non_dict_inputs_are_invalid(self):
        for req, ev in ((None, evidence()), (request(), []), ("request", "evidence")):
            with self.subTest(req=req, ev=ev):
                self.assertInvalid(req, ev)

    def test_unknown_keys_are_invalid_at_every_level(self):
        mutations = []
        req = request()
        req["requested_route"] = "one_render_upper_bound"
        mutations.append((req, evidence()))

        ev = evidence()
        ev["temporal_prediction"] = {"evidence_status": "current"}
        mutations.append((request(), ev))

        ev = evidence()
        ev["exact_cache"]["preview_only"] = True
        mutations.append((request(), ev))

        ev = evidence()
        ev["exact_cache"]["source_eligibility"]["trusted"] = True
        mutations.append((request(), ev))

        ev = evidence()
        ev["spatial75"]["one_render_gate_pass"] = True
        mutations.append((request(), ev))

        for req, ev in mutations:
            with self.subTest(req=req, ev=ev):
                self.assertInvalid(req, ev)

    def test_missing_keys_are_invalid_at_every_level(self):
        mutations = []
        req = request()
        del req["intent"]
        mutations.append((req, evidence()))

        ev = evidence()
        del ev["spatial75"]
        mutations.append((request(), ev))

        ev = evidence()
        del ev["exact_cache"]["artifact_request_identity"]
        mutations.append((request(), ev))

        ev = evidence()
        del ev["exact_cache"]["source_eligibility"]["artifact_verified"]
        mutations.append((request(), ev))

        ev = evidence()
        del ev["spatial75"]["fresh_two_render_gate_pass"]
        mutations.append((request(), ev))

        ev = evidence()
        del ev["spatial75"]["request_identity"]
        mutations.append((request(), ev))

        for req, ev in mutations:
            with self.subTest(req=req, ev=ev):
                self.assertInvalid(req, ev)

    def test_unknown_intent_and_status_enums_are_invalid(self):
        req = request("preview")
        self.assertInvalid(req, evidence())

        ev = evidence()
        ev["exact_cache"]["evidence_status"] = "available"
        self.assertInvalid(request(), ev)

        ev = evidence()
        ev["spatial75"]["evidence_status"] = "success"
        self.assertInvalid(request(), ev)

    def test_bool_fields_reject_ints_and_strings(self):
        paths = (
            ("exact_cache", "source_eligibility", "experimental_preview"),
            ("exact_cache", "source_eligibility", "production_ready"),
            ("exact_cache", "source_eligibility", "artifact_verified"),
            ("exact_cache", "source_eligibility", "billing_eligible"),
            ("spatial75", "fresh_two_render_gate_pass"),
        )
        for path in paths:
            for bad in (0, 1, "true", None):
                with self.subTest(path=path, bad=bad):
                    ev = evidence()
                    target = ev
                    for key in path[:-1]:
                        target = target[key]
                    target[path[-1]] = bad
                    self.assertInvalid(request(), ev)

    def test_identity_types_and_ambiguous_strings_are_invalid(self):
        for bad in (None, 1, "", " padded ", "x" * 513):
            with self.subTest(request_identity=bad):
                req = request()
                req["request_identity"] = bad
                self.assertInvalid(req, evidence())

        for bad in (1, "", " padded ", "x" * 513):
            with self.subTest(artifact_identity=bad):
                ev = evidence()
                ev["exact_cache"]["artifact_request_identity"] = bad
                self.assertInvalid(request(), ev)

    def test_nested_container_types_are_invalid(self):
        ev = evidence()
        ev["exact_cache"] = []
        self.assertInvalid(request(), ev)

        ev = evidence()
        ev["exact_cache"]["source_eligibility"] = None
        self.assertInvalid(request(), ev)

        ev = evidence()
        ev["spatial75"] = "current"
        self.assertInvalid(request(), ev)

    def test_incoherent_source_eligibility_is_invalid(self):
        ev = current_exact()
        ev["exact_cache"]["source_eligibility"].update(
            {
                "production_ready": True,
                "artifact_verified": False,
            }
        )
        self.assertInvalid(request(), ev)

        ev = current_exact()
        ev["exact_cache"]["source_eligibility"]["billing_eligible"] = True
        self.assertInvalid(request(), ev)

    def test_valid_final_intent_survives_corrupt_internal_evidence(self):
        ev = evidence()
        del ev["spatial75"]
        result = policy.decide_customer_render(request(policy.INTENT_FINAL), ev)
        self.assertEqual(result["request_intent"], policy.INTENT_FINAL)
        self.assertEqual(result["delivered_route"], policy.ROUTE_BASIC_CYCLES)
        self.assertEqual(
            result["billability_ceiling"],
            policy.BILLABILITY_STANDARD_CUSTOMER_RENDER,
        )

    def test_valid_preview_intent_with_corrupt_evidence_still_falls_back(self):
        ev = evidence()
        del ev["exact_cache"]
        result = policy.decide_customer_render(
            request(policy.INTENT_EXPERIMENTAL_PREVIEW), ev
        )
        self.assertEqual(
            result["request_intent"], policy.INTENT_EXPERIMENTAL_PREVIEW
        )
        self.assertEqual(result["attempted_route"], policy.ROUTE_BASIC_CYCLES)
        self.assertEqual(result["delivered_route"], policy.ROUTE_BASIC_CYCLES)
        self.assertEqual(result["fallback_reason"], "invalid_policy_input")
        self.assertEqual(
            result["billability_ceiling"], policy.BILLABILITY_NON_BILLABLE
        )


if __name__ == "__main__":
    unittest.main()
