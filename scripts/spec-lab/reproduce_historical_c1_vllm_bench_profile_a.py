#!/usr/bin/env python3
"""Bounded Profile A replay of the historical Metal C1/K3 divergence.

This is a correctness diagnostic, never a performance benchmark.  It uses the
*installed* pre-fallback vLLM-Metal wheel in clean child processes and drives
the server through the real ``vllm bench serve`` OpenAI-completions client
path.  That matters because the original client leaves ``--logprobs`` unset
(``null`` on the wire in this vLLM client) and streams SSE responses.  Earlier
endpoint diagnostics deliberately used a non-streaming token-ID observability
path, so they cannot settle this protocol question.

Normal invocations are preflight-only.  ``--execute`` is an explicit operator
action which creates four fresh servers in ABBA order (baseline, ngram, ngram,
baseline), saves write-once artifacts, and emits a diagnostic-only receipt.
The harness never derives, prints, or promotes a speed multiplier.

It intentionally does not import vLLM in the parent process.  Server, bench,
runtime-probe, and offline-tokenization children run from ``/private/tmp``
with ``PYTHONPATH`` and source-build variables removed, so a checked-out fork
cannot shadow the installed wheel.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator, Mapping, Sequence
import urllib.error
import urllib.request


SCHEMA = "cx.vllm_metal.historical_c1_vllm_bench_profile_a.v1"
RUNTIME_SCHEMA = "cx.vllm_metal.historical_c1_vllm_bench_runtime.v1"
TOKEN_SCHEMA = "cx.vllm_metal.historical_c1_vllm_bench_tokenization.v1"
FIXTURE_SCHEMA = "cx.vllm_metal.sonnet_parity_fixture.v1"

HISTORICAL_METAL_GIT_HEAD = "4c18ee0e6e3ce2b594ab114d0a53ca24eafb1d58"
HISTORICAL_VLLM_METAL_VERSION = "0.3.0.dev20260713103604"
HISTORICAL_VLLM_VERSION = "0.24.0+cpu"
HISTORICAL_MODEL_REVISION = "08231374eeacb049a0eade7922910865b8fce912"
HISTORICAL_NGRAM_SHA256 = "ae08d619c4daefb1df1b383fd63ef085eaf219ddc687a151451ad3fc3096fb9b"
HISTORICAL_SONNET_SHA256 = "d58663195ba6780da5f029b920c7ac00cad1e435ee1df7e03bf9ec2470f8dea4"
HISTORICAL_NUM_PROMPTS = 16
HISTORICAL_WARMUPS = 2
HISTORICAL_INPUT_LEN = 550
HISTORICAL_PREFIX_LEN = 200
HISTORICAL_OUTPUT_LEN = 150
HISTORICAL_MAX_MODEL_LEN = 2048
HISTORICAL_MAX_NUM_SEQS = 32
HISTORICAL_MAX_NUM_BATCHED_TOKENS = 2048
HISTORICAL_MEMORY_FRACTION = "0.5"
HISTORICAL_DIVERGENCE_REQUEST = 12
HISTORICAL_DIVERGENCE_INDEX = 92
HISTORICAL_BASELINE_TOKEN = 1875
HISTORICAL_NGRAM_TOKEN = 702
HISTORICAL_BASELINE_TOKEN_ROWS_SHA256 = "79ac15c1a7863250d72d7fde4796fdbc745506bcd783d45b1d2b0b029bffa8f6"
HISTORICAL_NGRAM_TOKEN_ROWS_SHA256 = "d3b58496fed523d9008c8d66acbdbebbbdf8a1758dca6a18d364bd21728438df"
HISTORICAL_PROMPT_LENGTHS = (503, 496, 509, 502, 515, 489, 503, 503, 507, 505, 523, 506, 500, 504, 505, 498)
MAX_JSON_BYTES = 64 << 20
MAX_LOG_BYTES = 64 << 20
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PIN_RE = re.compile(r"^[0-9a-f]{40}$")


class ProfileAError(RuntimeError):
    """The profile cannot provide a trustworthy diagnostic observation."""


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProfileAError("canonical_json_failed") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ProfileAError(f"duplicate_json_key_{key}")
        output[key] = value
    return output


def _reject_constant(value: str) -> None:
    raise ProfileAError(f"nonfinite_json_constant_{value}")


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ProfileAError(f"{label}_must_be_absolute_regular_file")
    raw = path.read_bytes()
    if not raw or len(raw) > MAX_JSON_BYTES:
        raise ProfileAError(f"{label}_invalid_size")
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ProfileAError) as exc:
        raise ProfileAError(f"{label}_invalid_json") from exc
    if not isinstance(value, dict):
        raise ProfileAError(f"{label}_root_must_be_object")
    return value, raw


def _require_file(value: str | Path, label: str, *, executable: bool = False) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ProfileAError(f"{label}_does_not_resolve") from exc
    if not path.is_file() or path.is_symlink() or (executable and not os.access(path, os.X_OK)):
        raise ProfileAError(f"{label}_must_be_regular_file")
    return path


def _require_dir(value: str | Path, label: str) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ProfileAError(f"{label}_does_not_resolve") from exc
    if not path.is_dir() or path.is_symlink():
        raise ProfileAError(f"{label}_must_be_directory")
    return path


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except (OSError, ValueError):
        return False
    return True


def _tool_path() -> Path:
    return Path(__file__).resolve()


def _source_root() -> Path:
    return _tool_path().parents[2]


def _clean_cwd() -> Path:
    """Use an external working directory, never the checked-out source tree."""
    preferred = Path("/private/tmp")
    if preferred.is_dir() and not preferred.is_symlink():
        return preferred.resolve()
    return Path(tempfile.gettempdir()).resolve()


def _clean_runtime_env() -> dict[str, str]:
    """Return an execution environment which cannot import the source tree."""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env.pop("VLLM_METAL_BUILD_FROM_SOURCE", None)
    env.update(
        {
            "PYTHONHASHSEED": "0",
            "PYTHONSAFEPATH": "1",
            "VLLM_ENABLE_V1_MULTIPROCESSING": "0",
            "VLLM_METAL_USE_PAGED_ATTENTION": "1",
            "VLLM_METAL_MEMORY_FRACTION": HISTORICAL_MEMORY_FRACTION,
            "VLLM_PLUGINS": "metal",
        }
    )
    return env


def expected_environment() -> dict[str, str | None]:
    return {
        "PYTHONHASHSEED": "0",
        "PYTHONSAFEPATH": "1",
        "PYTHONPATH": None,
        "PYTHONHOME": None,
        "VLLM_ENABLE_V1_MULTIPROCESSING": "0",
        "VLLM_METAL_USE_PAGED_ATTENTION": "1",
        "VLLM_METAL_MEMORY_FRACTION": HISTORICAL_MEMORY_FRACTION,
        "VLLM_PLUGINS": "metal",
        "VLLM_METAL_BUILD_FROM_SOURCE": None,
    }


def _profile_fixture(model: Path, sonnet: Path, fixture: Path) -> dict[str, Any]:
    value, raw = _read_json(fixture, "fixture")
    if value.get("schema") != FIXTURE_SCHEMA:
        raise ProfileAError("fixture_schema_invalid")
    source = value.get("source")
    model_record = value.get("model")
    sampler = value.get("sampler")
    if not isinstance(source, dict) or not isinstance(model_record, dict) or not isinstance(sampler, dict):
        raise ProfileAError("fixture_sections_invalid")
    if source.get("sonnet_text_sha256") != HISTORICAL_SONNET_SHA256:
        raise ProfileAError("fixture_sonnet_hash_not_historical")
    if sha256_file(sonnet) != HISTORICAL_SONNET_SHA256:
        raise ProfileAError("sonnet_dataset_hash_not_historical")
    if str(model) != model_record.get("path"):
        raise ProfileAError("model_path_differs_from_fixture")
    for field, filename in (
        ("model_safetensors_sha256", "model.safetensors"),
        ("tokenizer_json_sha256", "tokenizer.json"),
        ("config_json_sha256", "config.json"),
    ):
        expected = model_record.get(field)
        if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
            raise ProfileAError(f"fixture_{field}_invalid")
        if sha256_file(_require_file(model / filename, filename)) != expected:
            raise ProfileAError(f"model_{filename}_hash_differs_from_fixture")
    if (
        sampler.get("algorithm") != "vllm_sonnet_v1_random_choices"
        or sampler.get("seed") != 0
        or sampler.get("input_len") != HISTORICAL_INPUT_LEN
        or sampler.get("prefix_len") != HISTORICAL_PREFIX_LEN
    ):
        raise ProfileAError("fixture_sampler_not_historical")
    return {
        "path": str(fixture),
        "sha256": sha256_bytes(raw),
        "source_sha256": HISTORICAL_SONNET_SHA256,
        "model": {
            "model_safetensors_sha256": model_record["model_safetensors_sha256"],
            "tokenizer_json_sha256": model_record["tokenizer_json_sha256"],
            "config_json_sha256": model_record["config_json_sha256"],
            "chat_template_sha256": model_record.get("chat_template_sha256"),
        },
    }


def server_command(vllm_bin: Path, model: Path, model_revision: str, port: int | str, arm: str) -> list[str]:
    if arm not in {"baseline", "ngram"}:
        raise ProfileAError("invalid_arm")
    command = [
        str(vllm_bin),
        "serve",
        str(model),
        "--revision",
        model_revision,
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--max-model-len",
        str(HISTORICAL_MAX_MODEL_LEN),
        "--max-num-seqs",
        str(HISTORICAL_MAX_NUM_SEQS),
        "--max-num-batched-tokens",
        str(HISTORICAL_MAX_NUM_BATCHED_TOKENS),
        "--no-enable-prefix-caching",
        "--no-async-scheduling",
    ]
    if arm == "ngram":
        command.extend(
            [
                "--speculative-config",
                '{"method":"ngram","num_speculative_tokens":3,"prompt_lookup_min":2,"prompt_lookup_max":4}',
            ]
        )
    return command


def bench_command(vllm_bin: Path, model: Path, sonnet: Path, port: int | str, result_dir: Path, filename: str) -> list[str]:
    """Build the real benchmark-client command, with no ``--logprobs`` flag."""
    return [
        str(vllm_bin),
        "bench",
        "serve",
        "--backend",
        "openai",
        "--base-url",
        f"http://127.0.0.1:{port}",
        "--endpoint",
        "/v1/completions",
        "--tokenizer",
        str(model),
        "--dataset-name",
        "sonnet",
        "--dataset-path",
        str(sonnet),
        "--num-prompts",
        str(HISTORICAL_NUM_PROMPTS),
        "--request-rate",
        "inf",
        "--max-concurrency",
        "1",
        "--num-warmups",
        str(HISTORICAL_WARMUPS),
        "--seed",
        "0",
        "--ignore-eos",
        "--temperature",
        "0",
        "--top-p",
        "1.0",
        "--top-k",
        "-1",
        "--sonnet-input-len",
        str(HISTORICAL_INPUT_LEN),
        "--sonnet-prefix-len",
        str(HISTORICAL_PREFIX_LEN),
        "--sonnet-output-len",
        str(HISTORICAL_OUTPUT_LEN),
        "--disable-tqdm",
        "--save-result",
        "--save-detailed",
        "--result-dir",
        str(result_dir),
        "--result-filename",
        filename,
    ]


def abba_order(repeats: int) -> tuple[tuple[str, int], ...]:
    if repeats < 2:
        raise ProfileAError("repeats_must_be_at_least_two")
    result: list[tuple[str, int]] = []
    for repeat in range(repeats):
        result.extend((arm, repeat) for arm in (("baseline", "ngram") if repeat % 2 == 0 else ("ngram", "baseline")))
    return tuple(result)


def build_manifest(args: argparse.Namespace) -> dict[str, Any]:
    vllm_bin = _require_file(args.vllm_bin, "vllm_bin", executable=True)
    python_bin = _require_file(vllm_bin.parent / "python", "vllm_python", executable=True)
    model = _require_dir(args.model, "model")
    sonnet = _require_file(args.sonnet_dataset, "sonnet_dataset")
    fixture = _require_file(args.fixture_manifest, "fixture_manifest")
    if not PIN_RE.fullmatch(args.model_revision or "") or args.model_revision != HISTORICAL_MODEL_REVISION:
        raise ProfileAError("model_revision_not_historical")
    if args.repeats < 2:
        raise ProfileAError("repeats_must_be_at_least_two")
    binding = _profile_fixture(model, sonnet, fixture)
    command_template = {
        arm: server_command(vllm_bin, model, args.model_revision, "{PORT}", arm)
        for arm in ("baseline", "ngram")
    }
    return {
        "schema": SCHEMA,
        "claim_scope": "historical_correctness_diagnostic_only_no_speed_or_promotion_claim",
        "promotion_eligible": False,
        "speed_claim": "none",
        "tool": {"path": str(_tool_path()), "sha256": sha256_file(_tool_path())},
        "runtime": {
            "vllm_bin": str(vllm_bin),
            "vllm_bin_sha256": sha256_file(vllm_bin),
            "python_bin": str(python_bin),
            "python_bin_sha256": sha256_file(python_bin),
            "clean_environment": expected_environment(),
            "working_directory": str(_clean_cwd()),
            "installed_wheel_required": True,
            "historical_versions": {"vllm": HISTORICAL_VLLM_VERSION, "vllm_metal": HISTORICAL_VLLM_METAL_VERSION},
            "historical_ngram_proposer_sha256": HISTORICAL_NGRAM_SHA256,
        },
        "fixture": binding,
        "model": {"path": str(model), "revision": args.model_revision},
        "workload": {
            "dataset_path": str(sonnet),
            "dataset_sha256": HISTORICAL_SONNET_SHA256,
            "dataset_name": "sonnet",
            "num_prompts": HISTORICAL_NUM_PROMPTS,
            "warmups": HISTORICAL_WARMUPS,
            "max_concurrency": 1,
            "request_rate": "inf",
            "temperature": 0.0,
            "top_p": 1.0,
            "top_k": -1,
            "ignore_eos": True,
            "seed": 0,
            "input_len": HISTORICAL_INPUT_LEN,
            "prefix_len": HISTORICAL_PREFIX_LEN,
            "output_len": HISTORICAL_OUTPUT_LEN,
        },
        "client_protocol": {
            "entrypoint": "vllm bench serve",
            "endpoint": "/v1/completions",
            "stream": True,
            "logprobs_cli_flag": "absent",
            "wire_logprobs": None,
            "stream_options_include_usage": True,
            "client_command_template": bench_command(vllm_bin, model, sonnet, "{PORT}", Path("{RUN_DIR}"), "{RESULT_FILENAME}"),
        },
        "server_profile": {
            "entrypoint": "direct_console_vllm_serve",
            "max_model_len": HISTORICAL_MAX_MODEL_LEN,
            "max_num_seqs": HISTORICAL_MAX_NUM_SEQS,
            "max_num_batched_tokens": HISTORICAL_MAX_NUM_BATCHED_TOKENS,
            "prefix_caching": False,
            "async_scheduling": False,
            "environment": expected_environment(),
            "commands": command_template,
        },
        "abba": [{"arm": arm, "repeat": repeat} for arm, repeat in abba_order(args.repeats)],
        "expected_signature": {
            "request_index": HISTORICAL_DIVERGENCE_REQUEST,
            "zero_based_output_token_index": HISTORICAL_DIVERGENCE_INDEX,
            "baseline_token_id": HISTORICAL_BASELINE_TOKEN,
            "ngram_token_id": HISTORICAL_NGRAM_TOKEN,
            "baseline_token_rows_sha256": HISTORICAL_BASELINE_TOKEN_ROWS_SHA256,
            "ngram_token_rows_sha256": HISTORICAL_NGRAM_TOKEN_ROWS_SHA256,
        },
    }


def _new_output_dir(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or path.exists() or path.is_symlink() or not path.parent.is_dir():
        raise ProfileAError("output_dir_must_be_new_absolute_directory")
    try:
        path.mkdir(mode=0o700)
    except OSError as exc:
        raise ProfileAError("output_dir_create_failed") from exc
    return path.resolve()


def _write_new(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink() or not path.parent.is_dir():
        raise ProfileAError("artifact_path_not_new")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                raise ProfileAError("artifact_short_write")
            view = view[count:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json(path: Path, value: Any) -> None:
    _write_new(path, canonical_json_bytes(value) + b"\n")


def _reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_json(url: str, timeout_seconds: int) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 -- loopback only
            raw = response.read(MAX_JSON_BYTES + 1)
            if response.status != 200 or len(raw) > MAX_JSON_BYTES:
                raise ProfileAError("endpoint_invalid_response")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProfileAError("endpoint_request_failed") from exc
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ProfileAError) as exc:
        raise ProfileAError("endpoint_invalid_json") from exc
    if not isinstance(value, dict):
        raise ProfileAError("endpoint_json_not_object")
    return value


def _wait_for_health(process: subprocess.Popen[Any], base_url: str, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ProfileAError("server_exited_before_health")
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=2) as response:  # noqa: S310 -- loopback only
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(0.25)
    raise ProfileAError("server_health_timeout")


def _terminate(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        process.terminate()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()
        process.wait(timeout=10)


@contextlib.contextmanager
def _server(command: Sequence[str], env: Mapping[str, str], log_path: Path) -> Iterator[subprocess.Popen[Any]]:
    with log_path.open("xb") as log_handle:
        process = subprocess.Popen(
            list(command),
            cwd=str(_clean_cwd()),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            yield process
        finally:
            _terminate(process)


def _read_log_snapshot(path: Path, arm: str) -> dict[str, Any]:
    raw = path.read_bytes()
    if len(raw) > MAX_LOG_BYTES:
        raise ProfileAError("server_log_exceeds_limit")
    text = raw.decode("utf-8", errors="replace")
    needles = {
        "max_model_len": "max_model_len=2048",
        "max_num_seqs": "max_num_seqs=32",
        "max_num_batched_tokens": "max_num_batched_tokens=2048",
        "prefix_cache_disabled": "enable_prefix_caching=False",
        "async_scheduling_disabled": "async_scheduling=False",
    }
    if arm == "ngram":
        needles["ngram"] = "ngram"
    matching = {
        key: [index for index, line in enumerate(text.splitlines()) if needle in line]
        for key, needle in needles.items()
    }
    return {"sha256": sha256_bytes(raw), "bytes": len(raw), "resolved_config_line_indexes": matching}


def _runtime_probe_payload(vllm_bin: Path) -> dict[str, Any]:
    """Execute only in a clean installed-wheel child; never starts an engine."""
    import vllm  # noqa: PLC0415
    import vllm_metal  # noqa: PLC0415

    root = _source_root()
    vllm_path = Path(vllm.__file__).resolve()
    metal_path = Path(vllm_metal.__file__).resolve()
    if _path_is_within(vllm_path, root) or _path_is_within(metal_path, root):
        raise ProfileAError("runtime_probe_imported_source_tree")
    if "site-packages" not in vllm_path.parts or "site-packages" not in metal_path.parts:
        raise ProfileAError("runtime_probe_not_installed_wheel")
    ngram = _require_file(metal_path.parent / "v1" / "ngram_proposer.py", "ngram_proposer")
    extensions = sorted((metal_path.parent / "metal").glob("_paged_ops*"))
    if not extensions:
        raise ProfileAError("runtime_probe_missing_paged_extension")
    client = _require_file(vllm_path.parent / "benchmarks" / "lib" / "endpoint_request_func.py", "bench_client")
    client_source = client.read_text(encoding="utf-8")
    client_contract = {
        "logprobs_none_path": '"logprobs": request_func_input.logprobs' in client_source,
        "stream_true_path": '"stream": True' in client_source,
        "stream_usage_path": '"include_usage": True' in client_source,
    }
    if not all(client_contract.values()):
        raise ProfileAError("installed_bench_client_protocol_differs")
    paths = [Path(entry).resolve() for entry in sys.path if entry]
    if any(_path_is_within(entry, root) for entry in paths):
        raise ProfileAError("runtime_probe_sys_path_contains_source_tree")
    def record_hash(name: str) -> str:
        try:
            distribution = importlib.metadata.distribution(name)
            return sha256_file(distribution._path / "RECORD")  # type: ignore[attr-defined]
        except (importlib.metadata.PackageNotFoundError, AttributeError, OSError) as exc:
            raise ProfileAError(f"runtime_probe_distribution_{name}_unbound") from exc
    return {
        "schema": RUNTIME_SCHEMA,
        "runtime_import_mode": "installed_wheel_only_clean_child",
        "environment": {key: os.environ.get(key) for key in expected_environment()},
        "vllm_bin": str(vllm_bin),
        "vllm_bin_sha256": sha256_file(vllm_bin),
        "python": str(Path(sys.executable).resolve()),
        "vllm_module": str(vllm_path),
        "vllm_metal_module": str(metal_path),
        "vllm_version": importlib.metadata.version("vllm"),
        "vllm_metal_version": importlib.metadata.version("vllm-metal"),
        "vllm_record_sha256": record_hash("vllm"),
        "vllm_metal_record_sha256": record_hash("vllm-metal"),
        "ngram_proposer": {"path": str(ngram), "sha256": sha256_file(ngram)},
        "paged_extensions": [{"path": str(path.resolve()), "sha256": sha256_file(path)} for path in extensions],
        "bench_client": {"path": str(client), "sha256": sha256_file(client), "protocol": client_contract},
        "platform": platform.platform(),
        "machine": platform.machine(),
    }


def _runtime_probe(args: argparse.Namespace) -> int:
    if not args._runtime_probe_output or not args._runtime_probe_vllm_bin:
        raise ProfileAError("runtime_probe_args_missing")
    output = Path(args._runtime_probe_output).resolve()
    _write_json(output, _runtime_probe_payload(_require_file(args._runtime_probe_vllm_bin, "vllm_bin", executable=True)))
    return 0


def _run_runtime_probe(manifest: Mapping[str, Any], output_dir: Path) -> dict[str, Any]:
    runtime = manifest["runtime"]
    python_bin = Path(str(runtime["python_bin"]))
    vllm_bin = Path(str(runtime["vllm_bin"]))
    result = output_dir / "runtime-probe.json"
    completed = subprocess.run(
        [str(python_bin), "-P", "-B", str(_tool_path()), "--_runtime-probe", "--_runtime-probe-vllm-bin", str(vllm_bin), "--_runtime-probe-output", str(result)],
        cwd=str(_clean_cwd()),
        env=_clean_runtime_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        timeout=120,
        check=False,
    )
    _write_new(output_dir / "runtime-probe.stdout.log", completed.stdout)
    _write_new(output_dir / "runtime-probe.stderr.log", completed.stderr)
    if completed.returncode != 0 or not result.exists():
        raise ProfileAError("runtime_probe_failed")
    probe, raw = _read_json(result, "runtime_probe")
    if (
        probe.get("schema") != RUNTIME_SCHEMA
        or probe.get("vllm_version") != HISTORICAL_VLLM_VERSION
        or probe.get("vllm_metal_version") != HISTORICAL_VLLM_METAL_VERSION
        or probe.get("ngram_proposer", {}).get("sha256") != HISTORICAL_NGRAM_SHA256
    ):
        raise ProfileAError("runtime_probe_historical_identity_failed")
    if probe.get("environment") != expected_environment():
        raise ProfileAError("runtime_probe_environment_failed")
    return {"path": str(result), "sha256": sha256_bytes(raw), "value": probe}


def _tokenize_result(args: argparse.Namespace) -> int:
    """Offline, non-generating tokenization child for a saved bench result."""
    result_path = _require_file(args._tokenize_result, "bench_result")
    model = _require_dir(args._tokenize_model, "model")
    sonnet = _require_file(args._tokenize_sonnet, "sonnet")
    output = Path(args._tokenize_output).resolve()
    result, _ = _read_json(result_path, "bench_result")
    texts = result.get("generated_texts")
    if not isinstance(texts, list) or len(texts) != HISTORICAL_NUM_PROMPTS or not all(isinstance(item, str) for item in texts):
        raise ProfileAError("bench_result_generated_texts_invalid")
    try:
        from transformers import AutoTokenizer  # noqa: PLC0415
    except ImportError as exc:
        raise ProfileAError("transformers_missing_for_offline_tokenization") from exc
    import random  # noqa: PLC0415
    tokenizer = AutoTokenizer.from_pretrained(str(model), local_files_only=True)
    lines = sonnet.read_text(encoding="utf-8").splitlines(keepends=True)
    base = "Pick as many lines as you can from these poem lines:\n"
    formatted_base = tokenizer.apply_chat_template([{"role": "user", "content": base}], add_generation_prompt=True, tokenize=False)
    base_offset = len(tokenizer(formatted_base).input_ids)
    average = sum(len(tokenizer(line).input_ids) for line in lines) / len(lines)
    source_count = round((HISTORICAL_INPUT_LEN - base_offset) / average)
    prefix_count = max(round((HISTORICAL_PREFIX_LEN - base_offset) / average), 0)
    rng = random.Random(0)
    prompts: list[str] = []
    while len(prompts) < HISTORICAL_NUM_PROMPTS:
        prompt = base + "".join(lines[:prefix_count] + rng.choices(lines, k=source_count - prefix_count))
        formatted = tokenizer.apply_chat_template([{"role": "user", "content": prompt}], add_generation_prompt=True, tokenize=False)
        if len(tokenizer(formatted).input_ids) <= HISTORICAL_INPUT_LEN:
            prompts.append(formatted)
    prompt_lengths = [len(tokenizer(prompt).input_ids) for prompt in prompts]
    if tuple(prompt_lengths) != HISTORICAL_PROMPT_LENGTHS or result.get("input_lens") != prompt_lengths:
        raise ProfileAError("bench_prompt_workload_differs_from_historical")
    rows: list[list[int]] = []
    for prompt, text in zip(prompts, texts, strict=True):
        prefix = tokenizer(prompt).input_ids
        full = tokenizer(prompt + text).input_ids
        row = [int(token) for token in full[len(prefix):]]
        if len(row) != HISTORICAL_OUTPUT_LEN:
            raise ProfileAError("offline_tokenization_output_length_differs")
        rows.append(row)
    summary = {
        "schema": TOKEN_SCHEMA,
        "generated_texts_sha256": sha256_bytes(canonical_json_bytes(texts)),
        "token_rows_sha256": sha256_bytes(canonical_json_bytes(rows)),
        "prompt_token_lengths": prompt_lengths,
        "output_token_lengths": [len(row) for row in rows],
        "request_12_token_92": rows[HISTORICAL_DIVERGENCE_REQUEST][HISTORICAL_DIVERGENCE_INDEX],
        "per_output_token_sha256": [sha256_bytes(canonical_json_bytes(row)) for row in rows],
        "tokenizer": {
            "class": type(tokenizer).__module__ + "." + type(tokenizer).__qualname__,
            "chat_template_sha256": sha256_bytes((tokenizer.chat_template or "").encode("utf-8")),
        },
    }
    _write_json(output, summary)
    return 0


def _run_tokenization(manifest: Mapping[str, Any], output_dir: Path, result_path: Path, arm: str, repeat: int) -> tuple[dict[str, Any], str]:
    runtime = manifest["runtime"]
    output = output_dir / f"tokens-{arm}-{repeat}.json"
    completed = subprocess.run(
        [str(runtime["python_bin"]), "-P", "-B", str(_tool_path()), "--_tokenize-result", str(result_path), "--_tokenize-model", str(manifest["model"]["path"]), "--_tokenize-sonnet", str(manifest["workload"]["dataset_path"]), "--_tokenize-output", str(output)],
        cwd=str(_clean_cwd()),
        env=_clean_runtime_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        timeout=300,
        check=False,
    )
    _write_new(output_dir / f"tokens-{arm}-{repeat}.stdout.log", completed.stdout)
    _write_new(output_dir / f"tokens-{arm}-{repeat}.stderr.log", completed.stderr)
    if completed.returncode != 0:
        raise ProfileAError("offline_tokenization_failed")
    value, raw = _read_json(output, "tokenization")
    return value, sha256_bytes(raw)


def _run_arm(manifest: Mapping[str, Any], output_dir: Path, arm: str, repeat: int, server_timeout: int, bench_timeout: int) -> dict[str, Any]:
    runtime = manifest["runtime"]
    port = _reserve_port()
    server_log = output_dir / f"server-{arm}-{repeat}.log"
    bench_log = output_dir / f"bench-{arm}-{repeat}.log"
    models_path = output_dir / f"models-{arm}-{repeat}.json"
    startup_path = output_dir / f"startup-{arm}-{repeat}.json"
    result_path = output_dir / f"bench-{arm}-{repeat}.json"
    command = server_command(Path(str(runtime["vllm_bin"])), Path(str(manifest["model"]["path"])), str(manifest["model"]["revision"]), port, arm)
    base_url = f"http://127.0.0.1:{port}"
    process_metadata: dict[str, Any] = {"argv": command, "pid": None}
    with _server(command, _clean_runtime_env(), server_log) as process:
        process_metadata["pid"] = process.pid
        _wait_for_health(process, base_url, server_timeout)
        _write_json(models_path, _http_json(f"{base_url}/v1/models", 20))
        _write_json(startup_path, _read_log_snapshot(server_log, arm))
        command_client = bench_command(Path(str(runtime["vllm_bin"])), Path(str(manifest["model"]["path"])), Path(str(manifest["workload"]["dataset_path"])), port, output_dir, result_path.name)
        with bench_log.open("xb") as handle:
            try:
                completed = subprocess.run(
                    command_client,
                    cwd=str(_clean_cwd()),
                    env=_clean_runtime_env(),
                    stdin=subprocess.DEVNULL,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    timeout=bench_timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ProfileAError("bench_timeout") from exc
        if completed.returncode != 0 or not result_path.is_file():
            raise ProfileAError("bench_failed")
    tokens, tokens_sha = _run_tokenization(manifest, output_dir, result_path, arm, repeat)
    expected_rows = HISTORICAL_BASELINE_TOKEN_ROWS_SHA256 if arm == "baseline" else HISTORICAL_NGRAM_TOKEN_ROWS_SHA256
    expected_token = HISTORICAL_BASELINE_TOKEN if arm == "baseline" else HISTORICAL_NGRAM_TOKEN
    if tokens.get("token_rows_sha256") != expected_rows or tokens.get("request_12_token_92") != expected_token:
        raise ProfileAError("historical_signature_not_reproduced")
    return {
        "arm": arm,
        "repeat": repeat,
        "process": process_metadata,
        "server_log": {"path": str(server_log), "sha256": sha256_file(server_log)},
        "bench_log": {"path": str(bench_log), "sha256": sha256_file(bench_log)},
        "models": {"path": str(models_path), "sha256": sha256_file(models_path)},
        "startup": {"path": str(startup_path), "sha256": sha256_file(startup_path)},
        "bench_result": {"path": str(result_path), "sha256": sha256_file(result_path)},
        "tokenization": {"path": str(output_dir / f"tokens-{arm}-{repeat}.json"), "sha256": tokens_sha, **tokens},
    }


def execute(manifest: Mapping[str, Any], output_dir: Path, server_timeout: int, bench_timeout: int) -> dict[str, Any]:
    runtime = _run_runtime_probe(manifest, output_dir)
    arms: list[dict[str, Any]] = []
    for item in manifest["abba"]:
        arms.append(_run_arm(manifest, output_dir, str(item["arm"]), int(item["repeat"]), server_timeout, bench_timeout))
    return {
        "schema": SCHEMA,
        "status": "PASS",
        "claim_scope": "historical_correctness_diagnostic_only_no_speed_or_promotion_claim",
        "promotion_eligible": False,
        "speed_claim": "none",
        "timing_metrics_interpreted": False,
        "manifest_sha256": sha256_file(output_dir / "manifest.json"),
        "runtime_probe": {"path": runtime["path"], "sha256": runtime["sha256"]},
        "arms": arms,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vllm-bin")
    parser.add_argument("--model")
    parser.add_argument("--model-revision")
    parser.add_argument("--fixture-manifest")
    parser.add_argument("--sonnet-dataset")
    parser.add_argument("--output-dir")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--execute", action="store_true", help="Explicitly launch bounded local diagnostic servers.")
    parser.add_argument("--server-timeout-seconds", type=int, default=600)
    parser.add_argument("--bench-timeout-seconds", type=int, default=3600)
    parser.add_argument("--_runtime-probe", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_runtime-probe-vllm-bin", help=argparse.SUPPRESS)
    parser.add_argument("--_runtime-probe-output", help=argparse.SUPPRESS)
    parser.add_argument("--_tokenize-result", help=argparse.SUPPRESS)
    parser.add_argument("--_tokenize-model", help=argparse.SUPPRESS)
    parser.add_argument("--_tokenize-sonnet", help=argparse.SUPPRESS)
    parser.add_argument("--_tokenize-output", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args._runtime_probe:
            return _runtime_probe(args)
        if args._tokenize_result:
            if not all((args._tokenize_model, args._tokenize_sonnet, args._tokenize_output)):
                raise ProfileAError("tokenization_args_missing")
            return _tokenize_result(args)
        required = ("vllm_bin", "model", "model_revision", "fixture_manifest", "sonnet_dataset", "output_dir")
        missing = [f"--{name.replace('_', '-')}" for name in required if not getattr(args, name)]
        if missing:
            raise ProfileAError("missing_required_" + ",".join(missing))
        if args.server_timeout_seconds <= 0 or args.bench_timeout_seconds <= 0:
            raise ProfileAError("timeouts_must_be_positive")
        output_dir = _new_output_dir(args.output_dir)
        manifest = build_manifest(args)
        _write_json(output_dir / "manifest.json", manifest)
        if not args.execute:
            _write_json(
                output_dir / "preflight.json",
                {
                    "schema": SCHEMA,
                    "status": "PREFLIGHT_ONLY",
                    "claim_scope": "no_server_started_no_speed_or_promotion_claim",
                    "manifest_sha256": sha256_file(output_dir / "manifest.json"),
                    "execution_requested": False,
                },
            )
            print("PROFILE_A_PREFLIGHT_READY", flush=True)
            return 0
        receipt_path = output_dir / "receipt.json"
        try:
            receipt = execute(manifest, output_dir, args.server_timeout_seconds, args.bench_timeout_seconds)
        except Exception as exc:
            receipt = {
                "schema": SCHEMA,
                "status": "FAIL",
                "claim_scope": "historical_correctness_diagnostic_only_no_speed_or_promotion_claim",
                "promotion_eligible": False,
                "speed_claim": "none",
                "error": str(exc),
                "manifest_sha256": sha256_file(output_dir / "manifest.json"),
            }
            _write_json(receipt_path, receipt)
            raise
        _write_json(receipt_path, receipt)
        print("PROFILE_A_HISTORICAL_C1_REPRODUCED", flush=True)
        return 0
    except ProfileAError as exc:
        print(f"PROFILE_A_FAIL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
