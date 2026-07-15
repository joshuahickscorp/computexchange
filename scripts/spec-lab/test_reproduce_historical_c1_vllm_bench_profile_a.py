#!/usr/bin/env python3
"""CPU-only contract tests for the bounded historical Profile A harness."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import reproduce_historical_c1_vllm_bench_profile_a as subject  # noqa: E402


class ProfileACommandTests(unittest.TestCase):
    def test_abba_starts_and_ends_with_different_arms(self) -> None:
        self.assertEqual(
            subject.abba_order(2),
            (("baseline", 0), ("ngram", 0), ("ngram", 1), ("baseline", 1)),
        )
        self.assertEqual(subject.abba_order(3)[-2:], (("baseline", 2), ("ngram", 2)))
        with self.assertRaisesRegex(subject.ProfileAError, "at_least_two"):
            subject.abba_order(1)

    def test_server_uses_direct_console_and_recovered_profile(self) -> None:
        command = subject.server_command(
            Path("/wheel/bin/vllm"), Path("/model"), subject.HISTORICAL_MODEL_REVISION, 19001, "ngram"
        )
        self.assertEqual(command[:2], ["/wheel/bin/vllm", "serve"])
        self.assertNotIn("-m", command)
        self.assertIn("--no-enable-prefix-caching", command)
        self.assertIn("--no-async-scheduling", command)
        self.assertIn("--speculative-config", command)
        config = command[command.index("--speculative-config") + 1]
        self.assertEqual(
            config,
            '{"method":"ngram","num_speculative_tokens":3,"prompt_lookup_min":2,"prompt_lookup_max":4}',
        )
        baseline = subject.server_command(
            Path("/wheel/bin/vllm"), Path("/model"), subject.HISTORICAL_MODEL_REVISION, 19002, "baseline"
        )
        self.assertNotIn("--speculative-config", baseline)

    def test_bench_uses_real_streaming_client_protocol_without_logprobs_flag(self) -> None:
        command = subject.bench_command(
            Path("/wheel/bin/vllm"), Path("/model"), Path("/sonnet.txt"), 19001, Path("/receipt"), "result.json"
        )
        self.assertEqual(command[:3], ["/wheel/bin/vllm", "bench", "serve"])
        self.assertNotIn("--logprobs", command)
        self.assertNotIn("--stream", command)  # Installed benchmark client owns stream=True.
        self.assertIn("--save-detailed", command)
        self.assertIn("--num-warmups", command)
        self.assertEqual(command[command.index("--num-warmups") + 1], "2")
        self.assertEqual(command[command.index("--max-concurrency") + 1], "1")
        self.assertEqual(command[command.index("--request-rate") + 1], "inf")
        self.assertEqual(command[command.index("--temperature") + 1], "0")

    def test_clean_environment_drops_source_import_injection(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "PYTHONPATH": "/unsafe/source",
                "PYTHONHOME": "/unsafe/python",
                "VLLM_METAL_BUILD_FROM_SOURCE": "1",
            },
            clear=False,
        ):
            clean = subject._clean_runtime_env()
        self.assertNotIn("PYTHONPATH", clean)
        self.assertNotIn("PYTHONHOME", clean)
        self.assertNotIn("VLLM_METAL_BUILD_FROM_SOURCE", clean)
        self.assertEqual(clean["VLLM_PLUGINS"], "metal")
        self.assertEqual(clean["VLLM_ENABLE_V1_MULTIPROCESSING"], "0")
        self.assertEqual(clean["VLLM_METAL_MEMORY_FRACTION"], "0.5")


class ProfileAManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cx-profile-a-")
        self.root = Path(self.temporary.name)
        self.bin_dir = self.root / "wheel" / "bin"
        self.bin_dir.mkdir(parents=True)
        self.vllm = self.bin_dir / "vllm"
        self.python = self.bin_dir / "python"
        for path in (self.vllm, self.python):
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            path.chmod(0o700)
        self.model = self.root / "model"
        self.model.mkdir()
        for name in ("model.safetensors", "tokenizer.json", "config.json"):
            (self.model / name).write_text(name, encoding="utf-8")
        self.sonnet = self.root / "sonnet.txt"
        self.sonnet.write_text("fixture\n", encoding="utf-8")
        self.fixture = self.root / "fixture.json"
        self.fixture.write_text("{}\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(
            vllm_bin=str(self.vllm),
            model=str(self.model),
            model_revision=subject.HISTORICAL_MODEL_REVISION,
            fixture_manifest=str(self.fixture),
            sonnet_dataset=str(self.sonnet),
            repeats=2,
        )

    def test_manifest_is_diagnostic_only_and_binds_client_protocol(self) -> None:
        binding = {
            "path": str(self.fixture),
            "sha256": "a" * 64,
            "source_sha256": subject.HISTORICAL_SONNET_SHA256,
            "model": {
                "model_safetensors_sha256": "b" * 64,
                "tokenizer_json_sha256": "c" * 64,
                "config_json_sha256": "d" * 64,
                "chat_template_sha256": "e" * 64,
            },
        }
        with mock.patch.object(subject, "_profile_fixture", return_value=binding):
            manifest = subject.build_manifest(self._args())
        self.assertEqual(manifest["speed_claim"], "none")
        self.assertFalse(manifest["promotion_eligible"])
        protocol = manifest["client_protocol"]
        self.assertTrue(protocol["stream"])
        self.assertEqual(protocol["logprobs_cli_flag"], "absent")
        self.assertIsNone(protocol["wire_logprobs"])
        self.assertEqual(manifest["abba"], [
            {"arm": "baseline", "repeat": 0},
            {"arm": "ngram", "repeat": 0},
            {"arm": "ngram", "repeat": 1},
            {"arm": "baseline", "repeat": 1},
        ])

    def test_write_new_is_immutable(self) -> None:
        path = self.root / "receipt.json"
        subject._write_new(path, b"first")
        with self.assertRaisesRegex(subject.ProfileAError, "not_new"):
            subject._write_new(path, b"second")
        self.assertEqual(path.read_bytes(), b"first")

    def test_default_main_is_preflight_only_and_never_calls_execute(self) -> None:
        output = self.root / "preflight-output"
        manifest = {"schema": subject.SCHEMA, "speed_claim": "none"}
        argv = [
            "--vllm-bin", str(self.vllm),
            "--model", str(self.model),
            "--model-revision", subject.HISTORICAL_MODEL_REVISION,
            "--fixture-manifest", str(self.fixture),
            "--sonnet-dataset", str(self.sonnet),
            "--output-dir", str(output),
        ]
        with mock.patch.object(subject, "build_manifest", return_value=manifest), mock.patch.object(
            subject, "execute", side_effect=AssertionError("physical execution must stay gated")
        ) as execute:
            self.assertEqual(subject.main(argv), 0)
        execute.assert_not_called()
        self.assertEqual((output / "manifest.json").is_file(), True)
        preflight = (output / "preflight.json").read_text(encoding="utf-8")
        self.assertIn("PREFLIGHT_ONLY", preflight)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
