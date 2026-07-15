#!/usr/bin/env python3
"""CPU-only tests for local vLLM endpoint-attestation binding."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import unittest


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import cx_vllm_endpoint_attestation_v1 as subject  # noqa: E402


def _sha(label: str) -> str:
    return subject.sha256_bytes(label.encode("utf-8"))


def _write(path: Path, value: object) -> None:
    path.write_bytes(subject.canonical_json_bytes(value) + b"\n")


class _ModelsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/v1/models":
            self.send_error(404)
            return
        raw = json.dumps({"object": "list", "data": [{"id": "fixture/model"}]}, sort_keys=True).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class EndpointAttestationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cx-endpoint-attestation-")
        self.root = Path(self.temporary.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _ModelsHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/v1/completions"
        self.engine = self._tree("engine", "engine.py", "ENGINE = 'fixture'\n")
        self.core = self._tree("core", "core.py", "CORE = 'fixture'\n")
        self.metal = self._tree("metal", "metal.py", "METAL = 'fixture'\n")
        self.log = self.root / "server.log"
        self.log.write_text("server started\n", encoding="utf-8")
        self.argv = [str(Path(sys.executable).resolve()), "serve", "/fixture/model", "--served-model-name", "fixture/model", "--host", "127.0.0.1", "--port", str(self.server.server_port)]
        self.launch_plan = self.root / "launch-plan.json"
        self.runtime_input = self.root / "runtime-input.json"
        self._write_inputs()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.temporary.cleanup()

    def _tree(self, name: str, filename: str, content: str) -> Path:
        root = self.root / name
        root.mkdir()
        (root / filename).write_text(content, encoding="utf-8")
        return root

    def _write_inputs(self) -> None:
        environment = {
            "VLLM_ENABLE_V1_MULTIPROCESSING": "0",
            "VLLM_METAL_USE_PAGED_ATTENTION": "1",
        }
        source_sha = _sha("source")
        launch = {
            "schema_version": 1,
            "kind": subject.LAUNCH_PLAN_KIND,
            "source_sha256": source_sha,
            "endpoint": {"url": self.endpoint, "timeout_secs": 5, "authorization_env": None},
            "model": {"model_id": "fixture/model", "model_revision": "a" * 40, "precision_id": "q4"},
            "launch": {"argv": self.argv, "environment": environment},
            "lane": {
                "name": "exact_request_reuse",
                "baseline": "target_only_direct_decode",
                "candidate": "cx_exact_response_cache_then_direct_target_on_miss",
                "prefix_cache_enabled": False,
                "speculative_decode_enabled": False,
                "stream": False,
                "return_token_ids": True,
            },
        }
        _write(self.launch_plan, launch)
        trees = {
            name: subject._digest_tree(path, name)
            for name, path in (("engine", self.engine), ("vllm_core", self.core), ("metal_runtime", self.metal))
        }
        trees["engine"]["engine_commit"] = "b" * 40
        runtime = {
            "schema_version": 1,
            "kind": subject.RUNTIME_INPUT_KIND,
            "source_sha256": source_sha,
            "host": {"system": "Darwin", "machine": "arm64"},
            **trees,
            "resolved_engine_config_sha256": _sha("config"),
            "model": {"model_id": "fixture/model", "model_revision": "a" * 40},
        }
        _write(self.runtime_input, runtime)

    def _probe(self, pid: int) -> dict[str, object]:
        return {"pid": pid, "started_at": "Tue Jul 15 06:00:00 2026", "command": shlex.join(self.argv), "argv": list(self.argv)}

    def _attest(self) -> tuple[Path, Path]:
        snapshot = self.root / "startup.snapshot"
        out = self.root / "attestation.json"
        result = subject.attest(
            launch_plan_path=self.launch_plan,
            runtime_input_path=self.runtime_input,
            startup_log_path=self.log,
            startup_log_snapshot_out=snapshot,
            pid=4242,
            attestation_out=out,
            process_probe=self._probe,
        )
        self.assertEqual(result["attestation"], str(out))
        return out, snapshot

    def test_attestation_binds_live_models_process_and_runtime(self) -> None:
        out, snapshot = self._attest()
        raw = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(raw["endpoint"]["served_model_id"], "fixture/model")
        self.assertEqual(raw["runtime"]["engine"]["engine_commit"], "b" * 40)
        self.assertEqual(raw["startup_log_snapshot"]["path"], str(snapshot))
        value = subject.validate_for_workload(
            out,
            endpoint_url=self.endpoint,
            model_id="fixture/model",
            model_revision="a" * 40,
            runtime={"resolved_engine_config_sha256": _sha("config")},
        )
        self.assertEqual(value["attestation_sha256"], raw["attestation_sha256"])

    def test_snapshot_mutation_and_process_argv_mismatch_fail_closed(self) -> None:
        out, snapshot = self._attest()
        snapshot.write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(subject.EndpointAttestationError, "snapshot_changed"):
            subject.validate_for_workload(
                out,
                endpoint_url=self.endpoint,
                model_id="fixture/model",
                model_revision="a" * 40,
                runtime={"resolved_engine_config_sha256": _sha("config")},
            )
        bad_snapshot = self.root / "bad.snapshot"
        bad_out = self.root / "bad-attestation.json"
        with self.assertRaisesRegex(subject.EndpointAttestationError, "command_differs"):
            subject.attest(
                launch_plan_path=self.launch_plan,
                runtime_input_path=self.runtime_input,
                startup_log_path=self.log,
                startup_log_snapshot_out=bad_snapshot,
                pid=4242,
                attestation_out=bad_out,
                process_probe=lambda pid: {"pid": pid, "started_at": "Tue Jul 15 06:00:00 2026", "command": "wrong", "argv": ["wrong"]},
            )


if __name__ == "__main__":
    raise SystemExit(unittest.main())
