#!/usr/bin/env python3

from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import launch_pinned  # noqa: E402
import validate_runtime_lock as validator  # noqa: E402


MODEL_BYTES = b"synthetic GGUF bytes used only by launcher unit tests\n"
PROMPT = "Write a deterministic one-sentence summary of speculative decoding."


def complete_candidate() -> dict:
    return {
        "schema_version": 1,
        "status": "candidate",
        "runtime": dict(validator.EXACT_RUNTIME),
        "model": {
            "catalog_id": "llama-3.2-1b-instruct-q4",
            "repository": "unsloth/Llama-3.2-1B-Instruct-GGUF",
            "revision": "0123456789abcdef0123456789abcdef01234567",
            "artifact_filename": "llama-3.2-1b-instruct-q4_k_m.gguf",
            "artifact_sha256": hashlib.sha256(MODEL_BYTES).hexdigest(),
            "tokenizer_repository": "meta-llama/Llama-3.2-1B-Instruct",
            "tokenizer_revision": "89abcdef0123456789abcdef0123456789abcdef",
            "served_model_name": "llama-3.2-1b-instruct-q4",
        },
        "execution": {
            "weight_format": "gguf",
            "quantization": "q4_k_m",
            "dtype": "float16",
            "tensor_parallel_size": 1,
            "pipeline_parallel_size": 1,
            "data_parallel_size": 1,
            "resolved_kv_cache_dtype": "float16",
            "max_model_len": 8192,
            "max_num_seqs": 32,
            "max_num_batched_tokens": 8192,
            "gpu_memory_utilization": 0.9,
            "seed": 0,
            "trust_remote_code": False,
            "attention": {"backend": "FLASH_ATTN", "flash_attn_version": 3},
            "compilation": {"cudagraph_mode": "FULL_AND_PIECEWISE", "mode": 3},
            "network": {"host": "127.0.0.1", "port": 8000},
        },
        "speculative_decoding": {
            "enabled": True,
            "method": "ngram",
            "num_speculative_tokens": 4,
            "prompt_lookup_min": 2,
            "prompt_lookup_max": 4,
        },
        "sampling": {
            "temperature": 0,
            "top_p": 1,
            "top_k": -1,
            "seed": 0,
            "n": 1,
            "presence_penalty": 0,
            "frequency_penalty": 0,
        },
        "canary": {
            "prompt": PROMPT,
            "prompt_sha256": hashlib.sha256(PROMPT.encode("utf-8")).hexdigest(),
            "max_tokens": 64,
            "expected_completion_sha256": hashlib.sha256(
                b"deterministic expected completion"
            ).hexdigest(),
            "warmup_requests": 10,
            "measured_requests": 100,
            "minimum_acceptance_rate": 0.5,
            "minimum_output_match_rate": 1,
        },
    }


class RuntimeLockValidationTests(unittest.TestCase):
    def test_complete_candidate_is_valid(self) -> None:
        lock = complete_candidate()
        self.assertIs(validator.validate_lock(lock), lock)

    def test_unknown_fields_are_rejected_at_every_boundary(self) -> None:
        mutations = [
            lambda lock: lock.__setitem__("surprise", True),
            lambda lock: lock["runtime"].__setitem__("tag", "v0.24.0"),
            lambda lock: lock["model"].__setitem__("branch", "main"),
            lambda lock: lock["execution"].__setitem__("auto_tune", True),
            lambda lock: lock["execution"]["attention"].__setitem__("fallback", True),
            lambda lock: lock["speculative_decoding"].__setitem__("fallback", True),
            lambda lock: lock["sampling"].__setitem__("best_of", 2),
            lambda lock: lock["canary"].__setitem__("note", "ignored"),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                lock = complete_candidate()
                mutation(lock)
                with self.assertRaises(validator.LockValidationError) as context:
                    validator.validate_lock(lock)
                self.assertIn("unknown field", str(context.exception))

    def test_missing_fields_are_rejected(self) -> None:
        paths = [
            ("runtime", "vllm_commit"),
            ("model", "artifact_sha256"),
            ("execution", "dtype"),
            ("sampling", "seed"),
            ("canary", "expected_completion_sha256"),
        ]
        for parent, field in paths:
            with self.subTest(path=f"{parent}.{field}"):
                lock = complete_candidate()
                del lock[parent][field]
                with self.assertRaises(validator.LockValidationError) as context:
                    validator.validate_lock(lock)
                self.assertIn("missing required field", str(context.exception))

    def test_floating_and_placeholder_identities_are_rejected(self) -> None:
        mutations = [
            lambda lock: lock["model"].__setitem__("revision", "main"),
            lambda lock: lock["model"].__setitem__(
                "tokenizer_revision", "REQUIRED_40_HEX_COMMIT"
            ),
            lambda lock: lock["runtime"].__setitem__(
                "container_image", "vllm/vllm-openai:v0.24.0"
            ),
            lambda lock: lock["execution"]["attention"].__setitem__(
                "backend", "AUTO"
            ),
            lambda lock: lock["speculative_decoding"].__setitem__(
                "method", "REQUIRED_MEASURED_METHOD"
            ),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                lock = complete_candidate()
                mutation(lock)
                with self.assertRaises(validator.LockValidationError):
                    validator.validate_lock(lock)

    def test_duplicate_keys_are_rejected_while_loading(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.json"
            path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
            with self.assertRaises(validator.LockValidationError) as context:
                validator.load_lock(path)
        self.assertIn("duplicate JSON key", str(context.exception))

    def test_checked_in_template_is_intentionally_invalid(self) -> None:
        template = validator.load_lock(HERE / "v0.24.0-candidate.template.json")
        errors = validator.collect_validation_errors(template)
        self.assertTrue(errors)
        self.assertTrue(any("unresolved placeholder" in error for error in errors))

    def test_checked_in_template_binds_the_cached_model_identity(self) -> None:
        template = validator.load_lock(HERE / "v0.24.0-candidate.template.json")
        self.assertEqual(
            template["model"],
            {
                "catalog_id": "llama-3.2-1b-instruct-q4",
                "repository": "unsloth/Llama-3.2-1B-Instruct-GGUF",
                "revision": "b69aef112e9f895e6f98d7ae0949f72ff09aa401",
                "artifact_filename": "Llama-3.2-1B-Instruct-Q4_K_M.gguf",
                "artifact_sha256": "3f5a22426976ab26cfe84dba63c1d08391717abb1af893e10f1b2968d862dcc1",
                "tokenizer_repository": "unsloth/Llama-3.2-1B-Instruct",
                "tokenizer_revision": "5a8abab4a5d6f164389b1079fb721cfab8d7126c",
                "served_model_name": "llama-3.2-1b-instruct-q4",
            },
        )

    def test_every_json_schema_object_is_closed(self) -> None:
        schema = json.loads((HERE / "runtime-lock.schema.json").read_text("utf-8"))

        def assert_closed(node: object, path: str = "$") -> None:
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIs(
                        node.get("additionalProperties"),
                        False,
                        f"open object schema at {path}",
                    )
                for key, value in node.items():
                    assert_closed(value, f"{path}.{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    assert_closed(value, f"{path}[{index}]")

        assert_closed(schema)

    def test_verified_runtime_identity_is_in_sync(self) -> None:
        schema = json.loads((HERE / "runtime-lock.schema.json").read_text("utf-8"))
        schema_runtime = schema["properties"]["runtime"]["properties"]
        schema_constants = {
            key: definition["const"] for key, definition in schema_runtime.items()
        }
        template = validator.load_lock(HERE / "v0.24.0-candidate.template.json")
        self.assertEqual(schema_constants, validator.EXACT_RUNTIME)
        self.assertEqual(template["runtime"], validator.EXACT_RUNTIME)
        self.assertEqual(template["status"], "candidate")


class PinnedLauncherTests(unittest.TestCase):
    def test_candidate_refuses_production_mode(self) -> None:
        with self.assertRaises(launch_pinned.LauncherError) as context:
            launch_pinned.enforce_mode(complete_candidate(), "production")
        self.assertIn("refusing production launch", str(context.exception))

    def test_artifact_hash_is_verified(self) -> None:
        lock = complete_candidate()
        with tempfile.TemporaryDirectory() as directory:
            model_dir = Path(directory)
            artifact = model_dir / lock["model"]["artifact_filename"]
            artifact.write_bytes(MODEL_BYTES)
            self.assertEqual(
                launch_pinned.verify_artifact(lock, model_dir), artifact.resolve()
            )
            artifact.write_bytes(b"changed")
            with self.assertRaises(launch_pinned.LauncherError) as context:
                launch_pinned.verify_artifact(lock, model_dir)
        self.assertIn("SHA-256 mismatch", str(context.exception))

    def test_command_contains_only_locked_speculative_configuration(self) -> None:
        lock = complete_candidate()
        artifact = Path("/models") / lock["model"]["artifact_filename"]
        command = launch_pinned.build_command(lock, artifact)
        self.assertEqual(command[:3], ["vllm", "serve", str(artifact)])
        self.assertNotIn("--trust-remote-code", command)
        expected_options = {
            "--served-model-name": lock["model"]["served_model_name"],
            "--tokenizer": lock["model"]["tokenizer_repository"],
            "--tokenizer-revision": lock["model"]["tokenizer_revision"],
            "--dtype": lock["execution"]["dtype"],
            "--load-format": lock["execution"]["weight_format"],
            "--kv-cache-dtype": lock["execution"]["resolved_kv_cache_dtype"],
            "--tensor-parallel-size": str(lock["execution"]["tensor_parallel_size"]),
            "--pipeline-parallel-size": str(
                lock["execution"]["pipeline_parallel_size"]
            ),
            "--data-parallel-size": str(lock["execution"]["data_parallel_size"]),
            "--max-model-len": str(lock["execution"]["max_model_len"]),
            "--max-num-seqs": str(lock["execution"]["max_num_seqs"]),
            "--max-num-batched-tokens": str(
                lock["execution"]["max_num_batched_tokens"]
            ),
            "--gpu-memory-utilization": str(
                lock["execution"]["gpu_memory_utilization"]
            ),
            "--seed": str(lock["execution"]["seed"]),
            "--host": lock["execution"]["network"]["host"],
            "--port": str(lock["execution"]["network"]["port"]),
        }
        for option, expected in expected_options.items():
            with self.subTest(option=option):
                self.assertEqual(command[command.index(option) + 1], expected)
        self.assertEqual(
            json.loads(command[command.index("--attention-config") + 1]),
            lock["execution"]["attention"],
        )
        self.assertEqual(
            json.loads(command[command.index("--compilation-config") + 1]),
            lock["execution"]["compilation"],
        )
        spec_index = command.index("--speculative-config") + 1
        self.assertEqual(
            json.loads(command[spec_index]),
            {
                "method": "ngram",
                "num_speculative_tokens": 4,
                "prompt_lookup_max": 4,
                "prompt_lookup_min": 2,
            },
        )

    def test_api_key_is_redacted(self) -> None:
        command = launch_pinned.build_command(
            complete_candidate(), Path("/models/model.gguf"), "super-secret"
        )
        rendered = launch_pinned._redacted_command(command)
        self.assertNotIn("super-secret", rendered)
        self.assertIn("<redacted>", rendered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
