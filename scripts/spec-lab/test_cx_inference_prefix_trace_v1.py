#!/usr/bin/env python3
"""Focused contract tests for CX native shared-prefix telemetry traces."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import cx_inference_prefix_trace_v1 as subject  # noqa: E402


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class PrefixTraceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tokens = [17, 23, 29]
        self.expectation = subject.TraceExpectation(
            bridge_id="cx_local_prefix_bridge",
            bridge_config_sha256=_sha("bridge-config"),
            trace_nonce=subject.derive_trace_nonce(
                request_digest_sha256=_sha("request"),
                prefix_token_ids_sha256=_sha("prefix"),
                runtime_sha256=_sha("runtime"),
                trial_identity="/private/tmp/cx/trial-0001/candidate/arm-result.json",
            ),
            request_digest_sha256=_sha("request"),
            prefix_token_ids_sha256=_sha("prefix"),
            prefix_token_count=512,
            runtime_sha256=_sha("runtime"),
            endpoint_attestation_sha256=_sha("endpoint-attestation"),
            tenant_scope_sha256=_sha("tenant"),
            prefix_prime_receipt_sha256=_sha("prefix-prime-receipt"),
            cache_instance_sha256=_sha("cache-instance"),
            cache_generation_sha256=_sha("cache-generation"),
            engine_input_token_ids_sha256=_sha("engine-input-tokens"),
            native_prefix_block_size=16,
        )

    def _artifact(
        self,
        *,
        hit: bool = True,
        response_cache_hit: bool = False,
        speculative_decode_used: bool = False,
        fallback_available: bool = True,
        fallback_used: bool | None = None,
        outcome: str | None = None,
    ) -> dict[str, object]:
        if fallback_used is None:
            fallback_used = not hit
        if outcome is None:
            outcome = (
                subject.OUTCOME_NATIVE_PREFIX_HIT
                if hit
                else subject.OUTCOME_FALLBACK_DIRECT_DECODE
            )
        return {
            "schema_version": subject.SCHEMA_VERSION,
            "kind": subject.TRACE_ARTIFACT_KIND,
            "bridge_id": self.expectation.bridge_id,
            "bridge_config_sha256": self.expectation.bridge_config_sha256,
            "trace_nonce": self.expectation.trace_nonce,
            "request_digest_sha256": self.expectation.request_digest_sha256,
            "prefix_token_ids_sha256": self.expectation.prefix_token_ids_sha256,
            "prefix_token_count": self.expectation.prefix_token_count,
            "runtime_sha256": self.expectation.runtime_sha256,
            "endpoint_attestation_sha256": self.expectation.endpoint_attestation_sha256,
            "tenant_scope_sha256": self.expectation.tenant_scope_sha256,
            "prefix_prime_receipt_sha256": self.expectation.prefix_prime_receipt_sha256,
            "cache_instance_sha256": self.expectation.cache_instance_sha256,
            "cache_generation_sha256": self.expectation.cache_generation_sha256,
            "engine_input_token_ids_sha256": self.expectation.engine_input_token_ids_sha256,
            "native_prefix_block_size": self.expectation.native_prefix_block_size,
            "native_cached_token_count": self.expectation.prefix_token_count if hit else 0,
            "completion_token_ids_sha256": subject.sha256_json(self.tokens),
            "cache_backend": subject.NATIVE_PREFIX_BACKEND,
            "prefix_cache_hit": hit,
            "response_cache_hit": response_cache_hit,
            "speculative_decode_used": speculative_decode_used,
            "direct_target_fallback_available": fallback_available,
            "direct_target_fallback_used": fallback_used,
            "outcome": outcome,
        }

    def _trace(self, artifact: dict[str, object]) -> dict[str, object]:
        trace = {
            **artifact,
            "kind": subject.TRACE_KIND,
            "trace_artifact_sha256": subject.sha256_bytes(subject.canonical_json_bytes(artifact)),
            "trace_sha256": "",
        }
        trace["trace_sha256"] = subject.trace_sha256(trace)
        return trace

    def _response(self, trace: dict[str, object]) -> dict[str, object]:
        return {
            "id": "cmpl-test-only",
            "choices": [{"index": 0, "text": "ignored", "token_ids": list(self.tokens)}],
            subject.TRACE_FIELD: trace,
        }

    def _validate(self, artifact: dict[str, object], *, trace: dict[str, object] | None = None) -> subject.ValidatedPrefixTrace:
        trace = trace or self._trace(artifact)
        return subject.validate_openai_response(
            self._response(trace),
            expectation=self.expectation,
            trace_artifact=subject.canonical_json_bytes(artifact),
        )

    def test_native_prefix_hit_requires_bound_trace_artifact_and_returns_hit(self) -> None:
        artifact = self._artifact(hit=True)
        result = self._validate(artifact)
        self.assertTrue(result.is_native_prefix_hit)
        self.assertEqual(result.outcome, subject.OUTCOME_NATIVE_PREFIX_HIT)
        self.assertEqual(result.completion_token_ids, tuple(self.tokens))
        self.assertEqual(
            result.trace_artifact_sha256,
            subject.sha256_bytes(subject.canonical_json_bytes(artifact)),
        )

    def test_explicit_native_prefix_nonhit_is_only_a_fallback_outcome(self) -> None:
        result = self._validate(self._artifact(hit=False))
        self.assertFalse(result.is_native_prefix_hit)
        self.assertEqual(result.outcome, subject.OUTCOME_FALLBACK_DIRECT_DECODE)

    def test_rejects_a_nonhit_that_does_not_report_direct_fallback(self) -> None:
        artifact = self._artifact(hit=False, fallback_used=False)
        with self.assertRaisesRegex(subject.PrefixTraceError, "prefix_nonhit_requires_direct_fallback"):
            self._validate(artifact)

    def test_rejects_response_cache_or_speculative_decode_even_when_prefix_hit(self) -> None:
        for changed in (
            {"response_cache_hit": True},
            {"speculative_decode_used": True},
        ):
            with self.subTest(changed=changed):
                artifact = self._artifact()
                artifact.update(changed)
                with self.assertRaisesRegex(subject.PrefixTraceError, "forbidden"):
                    self._validate(artifact)

    def test_rejects_nonce_and_trace_artifact_mismatches(self) -> None:
        artifact = self._artifact()
        trace = self._trace(artifact)
        trace["trace_nonce"] = "wrong-nonce"
        trace["trace_sha256"] = subject.trace_sha256(trace)
        with self.assertRaisesRegex(subject.PrefixTraceError, "artifact_field_mismatch_trace_nonce"):
            self._validate(artifact, trace=trace)

        artifact = self._artifact()
        trace = self._trace(artifact)
        trace["trace_artifact_sha256"] = _sha("wrong-artifact")
        trace["trace_sha256"] = subject.trace_sha256(trace)
        with self.assertRaisesRegex(subject.PrefixTraceError, "artifact_digest_mismatch"):
            self._validate(artifact, trace=trace)

    def test_rejects_completion_tokens_not_bound_to_trace(self) -> None:
        artifact = self._artifact()
        trace = self._trace(artifact)
        trace["completion_token_ids_sha256"] = _sha("wrong-output")
        trace["trace_sha256"] = subject.trace_sha256(trace)
        with self.assertRaisesRegex(subject.PrefixTraceError, "artifact_field_mismatch_completion_token_ids_sha256"):
            self._validate(artifact, trace=trace)

    def test_rejects_unbound_cache_generation_and_invalid_cached_token_counts(self) -> None:
        artifact = self._artifact()
        artifact["cache_generation_sha256"] = _sha("wrong-generation")
        with self.assertRaisesRegex(subject.PrefixTraceError, "expectation_mismatch_cache_generation_sha256"):
            self._validate(artifact)

        artifact = self._artifact(hit=True)
        artifact["native_cached_token_count"] = 0
        with self.assertRaisesRegex(subject.PrefixTraceError, "native_prefix_hit_requires_cached_tokens"):
            self._validate(artifact)

        artifact = self._artifact(hit=False)
        artifact["native_cached_token_count"] = 1
        with self.assertRaisesRegex(subject.PrefixTraceError, "prefix_nonhit_must_not_report_cached_tokens"):
            self._validate(artifact)

    def test_rejects_noncanonical_artifact_bytes(self) -> None:
        artifact = self._artifact()
        trace = self._trace(artifact)
        raw = json.dumps(artifact, sort_keys=True).encode("utf-8")
        with self.assertRaisesRegex(subject.PrefixTraceError, "must_use_canonical_json"):
            subject.validate_openai_response(
                self._response(trace),
                expectation=self.expectation,
                trace_artifact=raw,
            )

    def test_status_cli_is_explicitly_unmeasured(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(Path(subject.__file__).resolve()), "--status"],
            check=False,
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertEqual(value["measurement_status"], "unmeasured")
        self.assertEqual(value["response_field"], subject.TRACE_FIELD)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
