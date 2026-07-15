#!/usr/bin/env python3
"""CPU-only contract tests for vLLM exact-cache workload preparation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import cx_inference_exact_cache_runner_v1 as exact_cache  # noqa: E402
import prepare_vllm_exact_cache_workload_v1 as subject  # noqa: E402
import screen_inference_lane_abba as abba  # noqa: E402


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


@unittest.skipUnless(shutil.which("git"), "git is required for source identity preparation")
class PrepareExactCacheWorkloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cx-vllm-exact-prep-")
        self.root = Path(self.temporary.name)
        self.engine = self.root / "vllm-metal-cx"
        self.engine.mkdir()
        (self.engine / "vllm_metal").mkdir()
        (self.engine / "vllm_metal" / "runtime.py").write_text("METAL = 'fixture'\n", encoding="utf-8")
        (self.engine / "pyproject.toml").write_text("fixture = true\n", encoding="utf-8")
        for command in (
            ["git", "init", "-q", str(self.engine)],
            ["git", "-C", str(self.engine), "config", "user.email", "test@example.invalid"],
            ["git", "-C", str(self.engine), "config", "user.name", "CX Test"],
            ["git", "-C", str(self.engine), "add", "."],
            ["git", "-C", str(self.engine), "commit", "-qm", "fixture"],
        ):
            completed = subprocess.run(command, check=False, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
        self.core = self.root / "vllm-core"
        self.core.mkdir()
        (self.core / "completion.py").write_text("CORE = 'fixture'\n", encoding="utf-8")
        self.source = self.root / "source.json"
        self._write_source()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _source_value(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": subject.SOURCE_KIND,
            "model": {
                "model_id": "fixture/model",
                "model_revision": "a" * 40,
                "tokenizer_id": "fixture/tokenizer",
                "tokenizer_revision": "b" * 40,
                "weights_sha256": _sha("weights"),
                "tokenizer_sha256": _sha("tokenizer"),
                "precision_id": "q4-test",
            },
            "endpoint": {
                "url": "http://127.0.0.1:18080/v1/completions",
                "timeout_secs": 30,
                "authorization_env": None,
            },
            "workload": {
                "tenant_scope_sha256": _sha("tenant"),
                "sampling": {"temperature": 0, "top_p": 1, "seed": 7, "n": 1},
                "max_output_tokens": 16,
                "concurrency": 1,
                "requests": [
                    {
                        "prompt": "A pre-tokenized exact-cache request.",
                        "input_token_ids": [10, 11, 12, 13],
                        "response_reuse_authorized": True,
                    }
                ],
            },
            "launch": {
                "argv": [
                    "/opt/cx/vllm",
                    "serve",
                    "/opt/cx/model",
                    "--served-model-name",
                    "fixture/model",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "18080",
                    "--max-model-len",
                    "2048",
                    "--max-num-batched-tokens",
                    "2048",
                    "--max-num-seqs",
                    "32",
                    "--no-enable-prefix-caching",
                    "--no-async-scheduling",
                ],
                "environment": {
                    "VLLM_ENABLE_V1_MULTIPROCESSING": "0",
                    "VLLM_METAL_USE_PAGED_ATTENTION": "1",
                    "VLLM_METAL_MEMORY_FRACTION": "0.5",
                },
            },
        }

    def _write_source(self, value: dict[str, object] | None = None) -> None:
        self.source.write_bytes(subject.canonical_json_bytes(value or self._source_value()) + b"\n")

    def _prepare(self) -> dict[str, object]:
        return subject.prepare(
            source_path=self.source,
            engine_root=self.engine,
            vllm_core_root=self.core,
            metal_runtime_root=self.engine / "vllm_metal",
            workload_out=self.root / "workload.json",
            launch_plan_out=self.root / "launch-plan.json",
            runtime_input_out=self.root / "runtime-input.json",
            baseline_runtime_out=self.root / "baseline-runtime.json",
            candidate_runtime_out=self.root / "candidate-runtime.json",
            host={
                "system": "Darwin",
                "machine": "arm64",
                "hardware_model": "Mac16,1",
                "cpu_brand": "Apple M3 Ultra",
                "memory_bytes": "549755813888",
            },
        )

    def test_prepares_runner_compatible_workload_and_identical_runtime_pair(self) -> None:
        result = self._prepare()
        self.assertEqual(result["status"], "prepared_unmeasured")
        workload_path = self.root / "workload.json"
        baseline = json.loads((self.root / "baseline-runtime.json").read_text(encoding="utf-8"))
        candidate = json.loads((self.root / "candidate-runtime.json").read_text(encoding="utf-8"))
        self.assertEqual(baseline, candidate)
        abba._parse_runtime(baseline)
        workload = exact_cache.load_workload(workload_path, runtime=baseline)
        body = workload["requests"][0]["completion_request"]
        self.assertTrue(body["return_token_ids"])
        self.assertFalse(body["stream"])
        plan = json.loads((self.root / "launch-plan.json").read_text(encoding="utf-8"))
        self.assertFalse(plan["lane"]["prefix_cache_enabled"])
        self.assertFalse(plan["lane"]["speculative_decode_enabled"])

    def test_refuses_a_speculative_server_plan(self) -> None:
        source = self._source_value()
        launch = source["launch"]
        assert isinstance(launch, dict)
        argv = launch["argv"]
        assert isinstance(argv, list)
        argv.extend(["--speculative-config", '{"method":"ngram"}'])
        self._write_source(source)
        with self.assertRaisesRegex(subject.PreparationError, "disable_prefix_cache_async_and_speculation"):
            subject._parse_source(subject._read_source(self.source))


if __name__ == "__main__":
    unittest.main()
