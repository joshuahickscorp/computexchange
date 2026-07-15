#!/usr/bin/env python3
"""Integration tests for the physical exact-response-cache ABBA arm runner.

The target below is only a deterministic local HTTP test double.  The test
proves protocol behavior (a primed candidate never calls the target and a
cache miss directly falls back); it deliberately makes no performance claim.
"""

from __future__ import annotations

import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import cx_inference_exact_cache_runner_v1 as subject  # noqa: E402
import cx_inference_policy_v1 as policy  # noqa: E402
import cx_inference_receipt_v1 as strict_receipt  # noqa: E402
import cx_inference_resident_worker_v1 as resident_worker  # noqa: E402
import cx_vllm_endpoint_attestation_v1 as endpoint_attestation  # noqa: E402
import screen_inference_lane_abba as abba  # noqa: E402


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(subject.canonical_json_bytes(value) + b"\n")


class _TargetHandler(BaseHTTPRequestHandler):
    request_count = 0
    bodies: list[dict[str, object]] = []
    lock = threading.Lock()

    def do_POST(self) -> None:  # noqa: N802
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        body = json.loads(raw.decode("utf-8"))
        with self.lock:
            type(self).request_count += 1
            type(self).bodies.append(body)
        prompt = str(body["prompt"])
        token_ids = [100 + len(prompt), 200 + len(prompt)]
        token_field = "token_ids" if body.get("return_token_ids") is True else "cx_completion_token_ids"
        payload = {"choices": [{"index": 0, "text": "test-only", token_field: token_ids}]}
        encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class ExactCacheRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cx-exact-cache-runner-")
        self.root = Path(self.temporary.name)
        _TargetHandler.request_count = 0
        _TargetHandler.bodies = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _TargetHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/v1/completions"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.temporary.cleanup()

    def _runtime(self, config_char: str) -> dict[str, str]:
        return {
            "backend": "metal",
            "host_hardware_sha256": _sha("host"),
            "core_sha256": _sha("core"),
            "runtime_sha256": _sha("runtime"),
            "resolved_engine_config_sha256": config_char * 64,
            "engine_id": "vllm-metal-cx",
            "engine_commit": "a" * 40,
            "metal_runtime_sha256": _sha("metal"),
            "weights_sha256": _sha("weights"),
            "tokenizer_sha256": _sha("tokenizer"),
            "precision_id": "q4-test",
        }

    def _request(self, *, input_tokens: list[int], prompt: str) -> dict[str, object]:
        sampling = {"temperature": 0, "top_p": 1, "seed": 7, "n": 1}
        request: dict[str, object] = {
            "contract": policy.REQUEST_CONTRACT,
            "request_identity_sha256": "",
            "tenant_scope_sha256": _sha("tenant-a"),
            "model_id": "fixture/model",
            "model_revision": "b" * 40,
            "tokenizer_sha256": _sha("tokenizer"),
            "runtime_sha256": _sha("runtime"),
            "sampling_contract_sha256": subject.sha256_json(sampling),
            "input_token_ids_sha256": subject.sha256_json(input_tokens),
            "shared_prefix_token_ids_sha256": None,
            "shared_prefix_token_count": 0,
            "max_output_tokens": 16,
            "concurrency": 1,
            "intent": policy.INTENT_EXPERIMENTAL,
            "response_reuse_authorized": True,
            "prefix_reuse_authorized": False,
        }
        request["request_identity_sha256"] = policy.request_identity_sha256(request)
        return {
            "request_index": 0,
            "request": request,
            "input_token_ids": input_tokens,
            "completion_request": {
                "model": "fixture/model",
                "prompt": prompt,
                "max_tokens": 16,
                "temperature": 0,
                "top_p": 1,
                "seed": 7,
                "n": 1,
                "return_token_ids": True,
            },
        }

    def _workload(self) -> tuple[Path, dict[str, object]]:
        request = self._request(input_tokens=[10, 11, 12], prompt="exact cache physical endpoint")
        request_digest = subject._request_digest(request)
        sampling = {"temperature": 0, "top_p": 1, "seed": 7, "n": 1}
        logical = {
            "model": {
                "model_id": "fixture/model",
                "model_revision": "b" * 40,
                "tokenizer_id": "fixture/tokenizer",
                "tokenizer_revision": "b" * 40,
            },
            "corpus_sha256": subject.sha256_json(
                [
                    {
                        "request": request["request"],
                        "input_token_ids": request["input_token_ids"],
                        "completion_request": request["completion_request"],
                    }
                ]
            ),
            "request_digests": [request_digest],
            "request_order_sha256": subject.sha256_json([request_digest]),
            "input_token_ids_sha256": subject.sha256_json([request["input_token_ids"]]),
            "sampling_contract_sha256": subject.sha256_json(sampling),
            "sampling": sampling,
            "max_output_tokens": 16,
            "concurrency": 1,
            "reuse_contract": {
                "eligible_request_indexes": [0],
                "exact_request_key_schema_sha256": subject.EXACT_REQUEST_KEY_SCHEMA_SHA256,
                "shared_prefix_token_ids_sha256": None,
                "shared_prefix_token_count": 0,
                "required_eligible_hit_rate": 1.0,
            },
        }
        value = {
            "schema_version": subject.SCHEMA_VERSION,
            "kind": subject.WORKLOAD_KIND,
            "logical_work": logical,
            "endpoint": {"url": self.endpoint, "timeout_secs": 5, "authorization_env": None},
            "requests": [request],
        }
        path = self.root / "workload.json"
        _write_json(path, value)
        return path, value

    def _attestation(self, runtime: dict[str, str], *, stem: str) -> Path:
        snapshot = self.root / f"{stem}-startup.log.snapshot"
        snapshot.write_bytes(b"test-only vLLM startup snapshot\n")
        value: dict[str, object] = {
            "schema_version": endpoint_attestation.SCHEMA_VERSION,
            "kind": endpoint_attestation.KIND,
            "claim_scope": "physical_local_unattested_endpoint_binding_only",
            "launch_plan": {
                "path": str((self.root / f"{stem}-launch-plan.json").resolve()),
                "sha256": _sha(f"{stem}-launch-plan"),
                "source_sha256": _sha(f"{stem}-source"),
                "prepared_argv_sha256": _sha(f"{stem}-argv"),
                "sanitized_environment": {},
                "sanitized_environment_sha256": subject.sha256_json({}),
            },
            "endpoint": {
                "url": self.endpoint,
                "models_url": self.endpoint.replace("/v1/completions", "/v1/models"),
                "models_response_sha256": _sha(f"{stem}-models"),
                "served_model_id": "fixture/model",
                "model_revision": "b" * 40,
            },
            "process": {"test_only": True},
            "runtime": {
                "runtime_input_path": str((self.root / f"{stem}-runtime-input.json").resolve()),
                "runtime_input_sha256": _sha(f"{stem}-runtime-input"),
                "resolved_engine_config_sha256": runtime["resolved_engine_config_sha256"],
                "engine": {"test_only": True},
                "vllm_core": {"test_only": True},
                "metal_runtime": {"test_only": True},
            },
            "startup_log_snapshot": {
                "path": str(snapshot.resolve()),
                "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                "byte_count": snapshot.stat().st_size,
            },
            "attestation_sha256": "",
        }
        value["attestation_sha256"] = endpoint_attestation._attestation_sha256(value)
        path = self.root / f"{stem}-endpoint-attestation.json"
        _write_json(path, value)
        return path

    def _call(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(Path(subject.__file__).resolve()), *args],
            check=False,
            text=True,
            capture_output=True,
            timeout=30,
        )

    def _prime_and_manifests(self) -> tuple[Path, Path, Path, Path, Path]:
        workload, _ = self._workload()
        baseline_runtime = self.root / "baseline-runtime.json"
        candidate_runtime = self.root / "candidate-runtime.json"
        _write_json(baseline_runtime, self._runtime("c"))
        _write_json(candidate_runtime, self._runtime("c"))
        attestation = self._attestation(self._runtime("c"), stem="primary")
        cache = self.root / "cache.json"
        prime = self._call(
            "prime",
            "--workload",
            str(workload),
            "--runtime",
            str(baseline_runtime),
            "--endpoint-attestation",
            str(attestation),
            "--cache-out",
            str(cache),
        )
        self.assertEqual(prime.returncode, 0, prime.stderr)
        baseline_manifest = self.root / "baseline.json"
        candidate_manifest = self.root / "candidate.json"
        manifests = self._call(
            "emit-arm-manifests",
            "--workload",
            str(workload),
            "--cache-artifact",
            str(cache),
            "--endpoint-attestation",
            str(attestation),
            "--baseline-runtime",
            str(baseline_runtime),
            "--candidate-runtime",
            str(candidate_runtime),
            "--baseline-manifest-out",
            str(baseline_manifest),
            "--candidate-manifest-out",
            str(candidate_manifest),
            "--timeout-secs",
            "30",
        )
        self.assertEqual(manifests.returncode, 0, manifests.stderr)
        return workload, cache, attestation, baseline_manifest, candidate_manifest

    def test_real_http_baseline_and_cache_candidate_flow_through_abba_receipt(self) -> None:
        workload, cache, attestation, baseline_manifest, candidate_manifest = self._prime_and_manifests()
        self.assertEqual(_TargetHandler.request_count, 1, "only direct cache priming should call target")
        config = abba.RunConfig(
            baseline_manifest=baseline_manifest,
            candidate_manifest=candidate_manifest,
            work_root=self.root / "abba-work",
            receipt_out=self.root / "abba-receipt.json",
            trials=8,
            qualifying_receipt_out=self.root / "qualifying-receipt.json",
        )
        receipt = abba.benchmark(config)
        self.assertEqual(receipt["measurement_status"], "real_paired_trials_recorded_local_unattested")
        self.assertEqual(receipt["reuse_coverage"]["observed_hits"], 8)
        self.assertEqual(receipt["reuse_coverage"]["observed_eligible_hit_rate"], 1.0)
        self.assertFalse(receipt["promotion_gate"]["candidate_authorization_flags_all_true"])
        # One prime plus precisely eight baseline arms.  Every candidate arm
        # was served by the cache and did not touch the endpoint.
        self.assertEqual(_TargetHandler.request_count, 9)
        self.assertEqual(len(_TargetHandler.bodies), 9)
        self.assertEqual(_TargetHandler.bodies[0]["prompt"], "exact cache physical endpoint")
        self.assertIs(_TargetHandler.bodies[0]["return_token_ids"], True)
        qualifying_path = config.qualifying_receipt_out
        assert qualifying_path is not None
        qualifying = json.loads(qualifying_path.read_text(encoding="utf-8"))
        strict_receipt.validate_receipt(qualifying)
        strict_receipt.verify_artifact_bindings(qualifying, config.work_root)
        self.assertEqual(qualifying["lane"], "exact_request_reuse")
        self.assertEqual(qualifying["reuse"]["coverage"]["eligible_hits"], 8)
        self.assertEqual(qualifying["authorization"]["billing_eligible"], False)
        baseline_raw = json.loads(baseline_manifest.read_text(encoding="utf-8"))
        pins = baseline_raw["command"]["pinned_files"]
        self.assertIn(
            {
                "role": "endpoint_attestation",
                "path": str(attestation.resolve()),
                "sha256": subject._sha256_file(attestation),
            },
            pins,
        )
        parity = json.loads(
            (config.work_root / "qualifying-receipt-artifacts" / "parity.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(
            any(
                item["sha256"] == subject._sha256_file(attestation)
                for item in parity["source_binding"]["baseline_command_pins"]
            )
        )
        self.assertTrue(workload.is_file())
        self.assertTrue(cache.is_file())

    def test_resident_worker_charges_rpc_and_preserves_exact_cache_contract(self) -> None:
        """A warm service removes process lifecycle, not customer-visible work."""

        workload, cache, attestation, baseline_manifest, candidate_manifest = self._prime_and_manifests()
        work_root = self.root / "resident-abba-work"
        session = self.root / "resident-session.json"
        socket_path = self.root / "resident.sock"
        worker_command = [
            sys.executable,
            str(Path(resident_worker.__file__).resolve()),
            "serve",
            "--baseline-manifest",
            str(baseline_manifest),
            "--candidate-manifest",
            str(candidate_manifest),
            "--workload",
            str(workload),
            "--endpoint-attestation",
            str(attestation),
            "--cache-artifact",
            str(cache),
            "--work-root",
            str(work_root),
            "--trials",
            "8",
            "--socket-path",
            str(socket_path),
            "--session-out",
            str(session),
        ]
        worker = subprocess.Popen(
            worker_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if session.exists() and socket_path.exists():
                    resident_worker.load_session(session, require_socket=True)
                    break
                if worker.poll() is not None:
                    stdout, stderr = worker.communicate(timeout=1)
                    self.fail(f"resident worker exited early: {stdout}\n{stderr}")
                time.sleep(0.05)
            else:
                self.fail("resident worker did not create an attested session")

            config = abba.RunConfig(
                baseline_manifest=baseline_manifest,
                candidate_manifest=candidate_manifest,
                work_root=work_root,
                receipt_out=self.root / "resident-abba-receipt.json",
                trials=8,
                qualifying_receipt_out=self.root / "resident-qualifying-receipt.json",
                resident_session=session,
            )
            receipt = abba.benchmark(config)
            self.assertEqual(receipt["execution"]["execution_transport"], "resident_unix_rpc")
            self.assertEqual(receipt["timing_contract"]["starts_before"], "resident RPC request serialization")
            self.assertEqual(_TargetHandler.request_count, 9)
            self.assertTrue(all(arm["execution"]["transport"] == "resident_unix_rpc" for trial in receipt["trials"] for arm in trial["arms"].values()))
            qualifying = json.loads(config.qualifying_receipt_out.read_text(encoding="utf-8"))
            strict_receipt.validate_receipt(qualifying)
            strict_receipt.verify_artifact_bindings(qualifying, work_root)
            parity = json.loads(
                (work_root / "qualifying-receipt-artifacts" / "parity.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                parity["source_binding"]["execution_context"]["execution_transport"],
                "resident_unix_rpc",
            )
        finally:
            if worker.poll() is None:
                worker.send_signal(signal.SIGINT)
                try:
                    worker.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.communicate(timeout=10)
            else:
                worker.communicate(timeout=1)
            socket_path.unlink(missing_ok=True)

    def test_candidate_cache_identity_mismatch_uses_direct_target_fallback(self) -> None:
        workload, cache, _attestation, _baseline_manifest, candidate_manifest = self._prime_and_manifests()
        raw_cache = json.loads(cache.read_text(encoding="utf-8"))
        raw_cache["entries"][0]["request"]["tenant_scope_sha256"] = _sha("wrong-tenant")
        raw_cache["entries"][0]["request"]["request_identity_sha256"] = policy.request_identity_sha256(
            raw_cache["entries"][0]["request"]
        )
        semantic = subject._cache_semantic(raw_cache)
        raw_cache["cache_history_sha256"] = subject.sha256_json(semantic)
        # The candidate manifest pin deliberately prevents replacing a cache
        # artifact after manifest emission.  Build a new pair for the altered
        # artifact to exercise the runner's safe cache-miss behavior.
        mismatch_cache = self.root / "mismatch-cache.json"
        _write_json(mismatch_cache, raw_cache)
        baseline_runtime = self.root / "baseline-runtime-mismatch.json"
        candidate_runtime = self.root / "candidate-runtime-mismatch.json"
        _write_json(baseline_runtime, self._runtime("e"))
        _write_json(candidate_runtime, self._runtime("e"))
        attestation = self._attestation(self._runtime("e"), stem="mismatch")
        baseline_manifest = self.root / "baseline-mismatch.json"
        candidate_manifest = self.root / "candidate-mismatch.json"
        manifests = self._call(
            "emit-arm-manifests",
            "--workload",
            str(workload),
            "--cache-artifact",
            str(mismatch_cache),
            "--endpoint-attestation",
            str(attestation),
            "--baseline-runtime",
            str(baseline_runtime),
            "--candidate-runtime",
            str(candidate_runtime),
            "--baseline-manifest-out",
            str(baseline_manifest),
            "--candidate-manifest-out",
            str(candidate_manifest),
            "--timeout-secs",
            "30",
        )
        self.assertEqual(manifests.returncode, 0, manifests.stderr)
        trial = self.root / "single-candidate"
        trial.mkdir()
        result = trial / "arm-result.json"
        command = [
            str(Path(subject.__file__).resolve()),
            "run",
            "--arm",
            "candidate",
            "--arm-manifest",
            str(candidate_manifest),
            "--workload",
            str(workload.resolve()),
            "--endpoint-attestation",
            str(attestation.resolve()),
            "--cache-artifact",
            str(mismatch_cache.resolve()),
            "--result-out",
            str(result.resolve()),
        ]
        for stage in subject.STAGES:
            command.extend(
                [
                    f"--stage-{stage.replace('_', '-')}-out",
                    str((trial / f"{stage}.json").resolve()),
                ]
            )
        completed = subprocess.run(command, check=False, text=True, capture_output=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        arm = json.loads(result.read_text(encoding="utf-8"))
        self.assertTrue(arm["fallback"]["used"])
        self.assertEqual(arm["fallback"]["reason_code"], "exact_cache_miss")
        self.assertFalse(arm["reuse_outcomes"][0]["hit"])
        self.assertEqual(_TargetHandler.request_count, 2, "prime plus direct fallback")
        self.assertTrue(candidate_manifest.is_file())


if __name__ == "__main__":
    raise SystemExit(unittest.main())
