#!/usr/bin/env python3
"""CPU-only contract tests for shared-prefix workload preparation."""

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

import prepare_vllm_shared_prefix_workload_v1 as subject  # noqa: E402
import screen_inference_lane_abba as abba  # noqa: E402


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


@unittest.skipUnless(shutil.which("git"), "git is required for source identity preparation")
class PrepareSharedPrefixWorkloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cx-vllm-prefix-prep-")
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

    def _launch(self, *, port: int, prefix_enabled: bool) -> dict[str, object]:
        argv = [
            "/opt/cx/vllm",
            "serve",
            "/opt/cx/model",
            "--served-model-name",
            "fixture/model",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--max-model-len",
            "4096",
            "--max-num-batched-tokens",
            "4096",
            "--max-num-seqs",
            "32",
            "--no-async-scheduling",
            "--enable-prefix-caching" if prefix_enabled else "--no-enable-prefix-caching",
        ]
        return {
            "argv": argv,
            "environment": {
                "VLLM_ENABLE_V1_MULTIPROCESSING": "0",
                "VLLM_METAL_USE_PAGED_ATTENTION": "1",
                "VLLM_METAL_MEMORY_FRACTION": "0.5",
            },
        }

    def _source_value(self) -> dict[str, object]:
        prefix = [101, 102, 103, 104]
        return {
            "schema_version": subject.SCHEMA_VERSION,
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
            "endpoints": {
                "baseline": {
                    "url": "http://127.0.0.1:18100/v1/completions",
                    "timeout_secs": 30,
                    "authorization_env": None,
                },
                "candidate": {
                    "url": "http://127.0.0.1:18101/v1/completions",
                    "timeout_secs": 30,
                    "authorization_env": None,
                },
            },
            "workload": {
                "tenant_scope_sha256": _sha("tenant"),
                "sampling": {"temperature": 0, "top_p": 1, "seed": 7, "n": 1},
                "max_output_tokens": 32,
                "concurrency": 2,
                "shared_prefix": {
                    "prompt": "SYSTEM: stable shared prefix\\n",
                    "token_ids": prefix,
                },
                "requests": [
                    {
                        "prompt_suffix": "Question alpha?",
                        "suffix_token_ids": [201, 202],
                        "input_token_ids": prefix + [201, 202],
                        "response_reuse_authorized": False,
                        "prefix_reuse_authorized": True,
                    },
                    {
                        "prompt_suffix": "Question beta?",
                        "suffix_token_ids": [203, 204, 205],
                        "input_token_ids": prefix + [203, 204, 205],
                        "response_reuse_authorized": False,
                        "prefix_reuse_authorized": True,
                    },
                ],
            },
            "launches": {
                "baseline": self._launch(port=18100, prefix_enabled=False),
                "candidate": self._launch(port=18101, prefix_enabled=True),
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
            baseline_launch_plan_out=self.root / "baseline-launch-plan.json",
            candidate_launch_plan_out=self.root / "candidate-launch-plan.json",
            baseline_runtime_input_out=self.root / "baseline-runtime-input.json",
            candidate_runtime_input_out=self.root / "candidate-runtime-input.json",
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

    def test_prepares_shared_prefix_workload_and_separate_launch_runtime_artifacts(self) -> None:
        result = self._prepare()
        self.assertEqual(result["status"], "prepared_unmeasured")
        workload = json.loads((self.root / "workload.json").read_text(encoding="utf-8"))
        logical = abba._parse_logical_work(workload["logical_work"], "shared_prefix_reuse")
        self.assertEqual(logical.value["reuse_contract"]["eligible_request_indexes"], [0, 1])
        self.assertIsNone(logical.value["reuse_contract"]["exact_request_key_schema_sha256"])
        self.assertEqual(
            logical.value["reuse_contract"]["shared_prefix_token_ids_sha256"],
            workload["shared_prefix"]["token_ids_sha256"],
        )
        self.assertEqual(workload["shared_prefix"]["token_count"], 4)
        suffixes = []
        for row in workload["requests"]:
            self.assertEqual(
                row["input_token_ids"][: workload["shared_prefix"]["token_count"]],
                workload["shared_prefix"]["token_ids"],
            )
            self.assertFalse(row["request"]["response_reuse_authorized"])
            self.assertTrue(row["request"]["prefix_reuse_authorized"])
            self.assertTrue(row["completion_request"]["return_token_ids"])
            self.assertFalse(row["completion_request"]["stream"])
            suffixes.append(row["fresh_suffix_sha256"])
        self.assertEqual(len(set(suffixes)), len(suffixes))

        baseline_plan = json.loads((self.root / "baseline-launch-plan.json").read_text(encoding="utf-8"))
        candidate_plan = json.loads((self.root / "candidate-launch-plan.json").read_text(encoding="utf-8"))
        self.assertFalse(baseline_plan["lane"]["prefix_cache_enabled"])
        self.assertTrue(candidate_plan["lane"]["prefix_cache_enabled"])
        self.assertFalse(baseline_plan["lane"]["response_cache_enabled"])
        self.assertFalse(candidate_plan["lane"]["speculative_decode_enabled"])

        baseline_runtime = json.loads((self.root / "baseline-runtime.json").read_text(encoding="utf-8"))
        candidate_runtime = json.loads((self.root / "candidate-runtime.json").read_text(encoding="utf-8"))
        abba._parse_runtime(baseline_runtime)
        abba._parse_runtime(candidate_runtime)
        self.assertEqual(baseline_runtime["runtime_sha256"], candidate_runtime["runtime_sha256"])
        self.assertNotEqual(
            baseline_runtime["resolved_engine_config_sha256"],
            candidate_runtime["resolved_engine_config_sha256"],
        )
        baseline_input = json.loads((self.root / "baseline-runtime-input.json").read_text(encoding="utf-8"))
        candidate_input = json.loads((self.root / "candidate-runtime-input.json").read_text(encoding="utf-8"))
        self.assertEqual(baseline_input["runtime_identity_sha256"], candidate_input["runtime_identity_sha256"])
        self.assertEqual(baseline_input["arm"], "baseline")
        self.assertEqual(candidate_input["arm"], "candidate")

    def test_rejects_input_tokens_that_do_not_start_with_the_declared_prefix(self) -> None:
        source = self._source_value()
        workload = source["workload"]
        assert isinstance(workload, dict)
        requests = workload["requests"]
        assert isinstance(requests, list) and isinstance(requests[0], dict)
        requests[0]["input_token_ids"] = [999, 201, 202]
        self._write_source(source)
        with self.assertRaisesRegex(subject.PreparationError, "exact_prefix_plus_suffix"):
            subject._parse_source(subject.exact_prep._read_source(self.source))

    def test_rejects_duplicate_suffixes_response_reuse_and_speculation(self) -> None:
        source = self._source_value()
        workload = source["workload"]
        assert isinstance(workload, dict)
        requests = workload["requests"]
        assert isinstance(requests, list) and isinstance(requests[0], dict) and isinstance(requests[1], dict)
        requests[1] = dict(requests[0])
        self._write_source(source)
        with self.assertRaisesRegex(subject.PreparationError, "suffixes_must_be_distinct"):
            subject._parse_source(subject.exact_prep._read_source(self.source))

        source = self._source_value()
        workload = source["workload"]
        assert isinstance(workload, dict)
        requests = workload["requests"]
        assert isinstance(requests, list) and isinstance(requests[0], dict)
        requests[0]["response_reuse_authorized"] = True
        self._write_source(source)
        with self.assertRaisesRegex(subject.PreparationError, "response_reuse_forbidden"):
            subject._parse_source(subject.exact_prep._read_source(self.source))

        source = self._source_value()
        launches = source["launches"]
        assert isinstance(launches, dict) and isinstance(launches["candidate"], dict)
        candidate = launches["candidate"]
        argv = candidate["argv"]
        assert isinstance(argv, list)
        argv.extend(["--speculative-config", '{"method":"ngram"}'])
        self._write_source(source)
        with self.assertRaisesRegex(subject.PreparationError, "speculative_decode_forbidden"):
            subject._parse_source(subject.exact_prep._read_source(self.source))

    def test_rejects_arm_changes_other_than_prefix_cache_or_port(self) -> None:
        source = self._source_value()
        launches = source["launches"]
        assert isinstance(launches, dict) and isinstance(launches["candidate"], dict)
        candidate = launches["candidate"]
        argv = candidate["argv"]
        assert isinstance(argv, list)
        argv[argv.index("--max-num-seqs") + 1] = "64"
        self._write_source(source)
        with self.assertRaisesRegex(subject.PreparationError, "launches_must_differ_only_by_prefix_cache_and_port"):
            subject._parse_source(subject.exact_prep._read_source(self.source))

        source = self._source_value()
        launches = source["launches"]
        assert isinstance(launches, dict) and isinstance(launches["candidate"], dict)
        candidate = launches["candidate"]
        environment = candidate["environment"]
        assert isinstance(environment, dict)
        environment["VLLM_METAL_MEMORY_FRACTION"] = "0.8"
        self._write_source(source)
        with self.assertRaisesRegex(subject.PreparationError, "environment_mismatch"):
            subject._parse_source(subject.exact_prep._read_source(self.source))


if __name__ == "__main__":
    raise SystemExit(unittest.main())
