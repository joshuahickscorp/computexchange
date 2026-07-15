#!/usr/bin/env python3
"""Local/remote OpenAI-completions and speculative-policy experiment lane.

This tool has four deliberately separate execution classes:

* ``embedded-mock`` exercises the exact request/response contract used by the
  Rust vLLM adapter, including out-of-order choices.  It is protocol evidence,
  never inference throughput evidence.
* ``local-vllm-metal`` measures an already-running vLLM-Metal server on Apple
  Silicon.  It never installs or launches the heavy runtime.
* ``local-openai-compatible`` measures another explicitly identified loopback
  engine, such as a future CX Candle/Metal compatibility shim.
* ``remote-vllm-cuda`` measures an endpoint bound to the repository's strict
  CUDA runtime lock.  The observed wall time includes the client/network path;
  it is not a local-Mac or kernel-only CUDA measurement.

Every run also replays an engine-neutral online n-gram policy over the returned
token traces.  Those receipts have no counterfactual baseline and therefore
cannot manufacture a speedup claim.  Capture artifacts can be compared after
running baseline and speculative servers in separate processes, which is useful
on a unified-memory Mac where both models may not fit at once.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Sequence


HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
SCRIPTS = REPO / "scripts"
VLLM_DIR = REPO / "docker" / "vllm"
DEFAULT_LEDGER = (
    REPO / "docs" / "speed-lane-reports" / "spec-lab"
    / "local_vllm_spec_lab_ledger.jsonl"
)
PERFORMANCE_SCHEMA = REPO / "proof" / "performance" / "benchmark-manifest.schema.json"
MAX_HTTP_BYTES = 8 << 20
MAX_REQUEST_BYTES = 2 << 20
DEFAULT_TIMEOUT_S = 120.0
DEFAULT_PROMPTS = (
    "Repeat this exact JSON pattern four times: {\"stage\":\"draft\",\"ok\":true}.",
    "Continue the repeating sequence without commentary: alpha beta gamma alpha beta gamma",
    "Write a short loop that repeats draft, verify, accept, repair in that order.",
    "Summarize: speculative execution drafts work, verifies it, accepts a prefix, and repairs a mismatch.",
)
TARGETS = {
    "embedded-mock",
    "local-vllm-metal",
    "local-openai-compatible",
    "remote-vllm-cuda",
}
VARIANTS = {"auto", "baseline", "ngram", "suffix", "draft_model", "mtp", "other"}


sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(VLLM_DIR))

from cx_speculative_core import SpecReceipt, decide_branch  # noqa: E402
from performance_proof import percentile  # noqa: E402
import validate_runtime_lock as runtime_lock_validator  # noqa: E402


class LabError(ValueError):
    """A fail-closed experiment contract error."""


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_or_none(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise LabError(f"{label} must be a lowercase 64-character SHA-256")
    return value


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _duplicate_safe_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LabError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _load_json_bytes(raw: bytes, context: str) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_duplicate_safe_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                LabError(f"{context}: non-finite JSON value {value!r}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LabError(f"{context}: invalid UTF-8 JSON: {exc}") from exc


def _read_small(path: Path, limit: int, context: str) -> bytes:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LabError(f"{context}: cannot read {path}: {exc}") from exc
    if not raw:
        raise LabError(f"{context}: {path} is empty")
    if len(raw) > limit:
        raise LabError(f"{context}: {path} exceeds {limit} bytes")
    return raw


def _sysctl(name: str) -> str | None:
    try:
        result = subprocess.run(
            ["sysctl", "-n", name],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = (result.stdout or "").strip()
    return value or None


def host_identity() -> dict[str, Any]:
    system = platform.system()
    machine = platform.machine()
    chip = _sysctl("machdep.cpu.brand_string") or platform.processor() or machine
    sku = _sysctl("hw.model") or "unresolved"
    memory_raw = _sysctl("hw.memsize")
    memory_bytes = int(memory_raw) if memory_raw and memory_raw.isdigit() else None
    hardware = {
        "system": system,
        "machine": machine,
        "chip": chip,
        "sku": sku,
        "system_memory_bytes": memory_bytes,
    }
    return {
        **hardware,
        "os_release": platform.release(),
        "os_version": platform.version(),
        "python": platform.python_version(),
        "host_id_sha256": sha256_text(platform.node() or "unresolved-host"),
        "hardware_fingerprint_sha256": sha256_json(hardware),
    }


def _metadata_probe(python_bin: Path) -> dict[str, str | None]:
    code = (
        "import importlib.metadata as m,json\n"
        "out={}\n"
        "for n in ('vllm-metal','vllm','mlx'):\n"
        "  try: out[n]=m.version(n)\n"
        "  except m.PackageNotFoundError: out[n]=None\n"
        "print(json.dumps(out,sort_keys=True))\n"
    )
    try:
        result = subprocess.run(
            [str(python_bin), "-c", code],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return {}
        parsed = json.loads(result.stdout.strip().splitlines()[-1])
        return parsed if isinstance(parsed, dict) else {}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, IndexError):
        return {}


def _xcode_command_line_tools_available() -> bool:
    try:
        result = subprocess.run(
            ["/usr/bin/xcode-select", "-p"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    path = (result.stdout or "").strip()
    return result.returncode == 0 and bool(path) and Path(path).exists()


def inspect_vllm_metal(
    *,
    explicit_bin: str | None = None,
    environ: dict[str, str] | None = None,
    home: Path | None = None,
    workspace_parent: Path | None = None,
) -> dict[str, Any]:
    """Detect vLLM-Metal without importing, installing, or starting it."""

    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else home
    workspace_parent = REPO.parent if workspace_parent is None else workspace_parent
    candidates = [
        explicit_bin,
        environ.get("CX_VLLM_METAL_BIN"),
        str(workspace_parent / "vllm-metal-cx" / ".venv-vllm-metal" / "bin" / "vllm"),
        str(workspace_parent / "vllm-metal" / ".venv-vllm-metal" / "bin" / "vllm"),
        str(home / ".venv-vllm-metal" / "bin" / "vllm"),
        shutil.which("vllm"),
    ]
    executable: Path | None = None
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            executable = path.resolve()
            break

    metadata: dict[str, str | None] = {}
    if executable is not None:
        sibling_python = executable.parent / "python"
        if sibling_python.is_file() and os.access(sibling_python, os.X_OK):
            metadata = _metadata_probe(sibling_python)
    if not metadata:
        for name in ("vllm-metal", "vllm", "mlx"):
            try:
                metadata[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                metadata[name] = None

    system = platform.system()
    machine = platform.machine()
    eligible_host = system == "Darwin" and machine == "arm64"
    installed = executable is not None and bool(metadata.get("vllm-metal"))
    if not eligible_host:
        status = "unsupported_host"
    elif installed:
        status = "installed_not_server_attested"
    elif executable is not None:
        status = "vllm_cli_found_metal_plugin_unresolved"
    else:
        status = "not_installed"

    identity_input = {
        "executable_sha256": (
            hashlib.sha256(executable.read_bytes()).hexdigest()
            if executable is not None and executable.stat().st_size <= (64 << 20)
            else None
        ),
        "packages": metadata,
    }
    return {
        "schema_version": 1,
        "probe_kind": "vllm_metal_installation",
        "status": status,
        "eligible_host": eligible_host,
        "host": {"system": system, "machine": machine},
        "current_python": {
            "version": platform.python_version(),
            "machine": machine,
            "meets_documented_python_3_12": sys.version_info[:2] == (3, 12),
        },
        "installation": {
            "executable": str(executable) if executable else None,
            "packages": metadata,
            "runtime_identity_sha256": sha256_json(identity_input) if installed else None,
        },
        "xcode_command_line_tools": _xcode_command_line_tools_available(),
        "install_performed": False,
        "server_started": False,
        "docs": {
            "installation": "https://docs.vllm.ai/projects/vllm-metal/en/latest/installation/",
            "speculative_decoding": "https://docs.vllm.ai/projects/vllm-metal/en/latest/speculative_decoding/",
        },
        "server_command_template": "vllm serve MODEL --port 8000",
        "note": (
            "Detection only. The endpoint and its launch flags are not attested by "
            "finding an installation on disk."
        ),
    }


def _validate_request_contract(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LabError("request root must be an object")
    required = {
        "model", "prompt", "max_tokens", "temperature", "top_p", "seed", "n", "logprobs"
    }
    if set(value) != required:
        missing = sorted(required - set(value))
        unknown = sorted(set(value) - required)
        raise LabError(f"request fields mismatch; missing={missing}, unknown={unknown}")
    if not isinstance(value["model"], str) or not value["model"].strip():
        raise LabError("model must be a non-empty string")
    prompts = value["prompt"]
    if not isinstance(prompts, list) or not 1 <= len(prompts) <= 256:
        raise LabError("prompt must be an array of 1..256 strings")
    if any(not isinstance(prompt, str) or not prompt for prompt in prompts):
        raise LabError("every prompt must be a non-empty string")
    if any(len(prompt.encode("utf-8")) > 65536 for prompt in prompts):
        raise LabError("a prompt exceeds 65536 UTF-8 bytes")
    if type(value["max_tokens"]) is not int or not 1 <= value["max_tokens"] <= 4096:
        raise LabError("max_tokens must be an integer in [1,4096]")
    exact = {"temperature": 0.0, "top_p": 1.0, "seed": 0, "n": 1, "logprobs": 0}
    for field, expected in exact.items():
        actual = value[field]
        if isinstance(expected, float):
            if isinstance(actual, bool) or not isinstance(actual, (int, float)) or float(actual) != expected:
                raise LabError(f"{field} must equal {expected!r}")
        elif type(actual) is not type(expected) or actual != expected:
            raise LabError(f"{field} must equal {expected!r}")
    return value


def completion_request(model: str, prompts: Sequence[str], max_tokens: int) -> dict[str, Any]:
    return _validate_request_contract(
        {
            "model": model,
            "prompt": list(prompts),
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "top_p": 1.0,
            "seed": 0,
            "n": 1,
            "logprobs": 0,
        }
    )


def _mock_tokens(prompt: str, max_tokens: int) -> list[str]:
    patterns = (
        (" draft", " verify", " accept", " repair"),
        (" alpha", " beta", " gamma", " alpha"),
        (" {", '"ok"', ":", "true", "}"),
    )
    pattern = patterns[int(sha256_text(prompt)[:8], 16) % len(patterns)]
    return [pattern[index % len(pattern)] for index in range(max_tokens)]


def _mock_response(request: dict[str, Any]) -> dict[str, Any]:
    choices = []
    for index, prompt in enumerate(request["prompt"]):
        tokens = _mock_tokens(prompt, request["max_tokens"])
        choices.append(
            {"index": index, "text": "".join(tokens), "logprobs": {"tokens": tokens}}
        )
    # The production adapter promises to restore input order.  Reverse order is
    # intentional and makes the default smoke exercise that branch.
    choices.reverse()
    return {
        "id": "cx-local-mock",
        "object": "text_completion",
        "model": request["model"],
        "choices": choices,
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": sum(len(row["logprobs"]["tokens"]) for row in choices),
            "total_tokens": sum(len(row["logprobs"]["tokens"]) for row in choices),
        },
    }


def _mock_handler() -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "cx-vllm-contract-mock/1"

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def _reply(self, status: int, value: dict[str, Any]) -> None:
            body = canonical_json_bytes(value)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path == "/health":
                self._reply(200, {"status": "ok", "engine": "cx-contract-mock"})
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path != "/v1/completions":
                self._reply(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= MAX_REQUEST_BYTES:
                    raise LabError("invalid Content-Length")
                request = _load_json_bytes(self.rfile.read(length), "mock request")
                _validate_request_contract(request)
            except (LabError, ValueError) as exc:
                self._reply(400, {"error": str(exc)[:500]})
                return
            self._reply(200, _mock_response(request))

    return Handler


class EmbeddedMock:
    def __init__(self, port: int = 0):
        self.server = ThreadingHTTPServer(("127.0.0.1", port), _mock_handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "EmbeddedMock":
        self.thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@dataclass(frozen=True)
class MappedChoice:
    index: int
    text_sha256: str
    token_count: int
    token_source: str
    policy_tokens: tuple[str, ...]


@dataclass(frozen=True)
class RequestResult:
    latency_ms: float
    choices: tuple[MappedChoice, ...]
    output_sha256: str
    response_was_out_of_order: bool

    @property
    def output_tokens(self) -> int:
        return sum(choice.token_count for choice in self.choices)


def map_completion_response(value: Any, prompt_count: int) -> tuple[tuple[MappedChoice, ...], bool]:
    if not isinstance(value, dict) or not isinstance(value.get("choices"), list):
        raise LabError("response must contain a choices array")
    raw_choices = value["choices"]
    if len(raw_choices) != prompt_count:
        raise LabError(f"response returned {len(raw_choices)} choices for {prompt_count} prompts")
    usage = value.get("usage")
    aggregate_tokens: int | None = None
    if usage is not None:
        if not isinstance(usage, dict):
            raise LabError("response usage must be an object when present")
        if "completion_tokens" in usage:
            candidate = usage["completion_tokens"]
            if type(candidate) is not int or candidate < 0:
                raise LabError("usage.completion_tokens must be a non-negative integer")
            aggregate_tokens = candidate
    ordered: list[MappedChoice | None] = [None] * prompt_count
    raw_order: list[int] = []
    for raw in raw_choices:
        if not isinstance(raw, dict):
            raise LabError("each choice must be an object")
        index = raw.get("index")
        text = raw.get("text")
        if type(index) is not int or not 0 <= index < prompt_count:
            raise LabError(f"choice has invalid index {index!r}")
        if ordered[index] is not None:
            raise LabError(f"duplicate choice index {index}")
        if not isinstance(text, str):
            raise LabError(f"choice {index} text must be a string")
        raw_order.append(index)
        logprobs = raw.get("logprobs")
        tokens: list[str] | None = None
        if logprobs is not None:
            if not isinstance(logprobs, dict):
                raise LabError(f"choice {index} logprobs must be an object or null")
            if "tokens" in logprobs and logprobs["tokens"] is not None:
                candidate = logprobs["tokens"]
                if not isinstance(candidate, list) or not all(
                    isinstance(token, str) for token in candidate
                ):
                    raise LabError(f"choice {index} logprobs.tokens must be an array of strings")
                tokens = candidate
        if tokens is not None:
            token_count = len(tokens)
            token_source = "logprobs.tokens"
            policy_tokens = tuple(tokens)
        elif prompt_count == 1 and aggregate_tokens is not None:
            token_count = aggregate_tokens
            token_source = "usage.completion_tokens"
            # Aggregate usage establishes billing/throughput count, but it does
            # not expose token identities. Byte replay is a separately labeled
            # engine-neutral policy trace, never a model-token acceptance claim.
            policy_tokens = tuple(f"b:{byte:02x}" for byte in text.encode("utf-8"))
        else:
            raise LabError(
                f"choice {index} has no authoritative per-choice logprobs.tokens; "
                "aggregate usage is only assignable for a single choice"
            )
        ordered[index] = MappedChoice(
            index=index,
            text_sha256=sha256_text(text),
            token_count=token_count,
            token_source=token_source,
            policy_tokens=policy_tokens,
        )
    if any(choice is None for choice in ordered):
        raise LabError("response is missing one or more choice indexes")
    mapped = tuple(choice for choice in ordered if choice is not None)
    if aggregate_tokens is not None:
        counted = sum(choice.token_count for choice in mapped)
        if counted != aggregate_tokens:
            raise LabError(
                "response token metadata disagrees: per-choice total "
                f"{counted}, usage.completion_tokens {aggregate_tokens}"
            )
    return mapped, raw_order != list(range(prompt_count))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def request_completions(
    endpoint: str,
    body: dict[str, Any],
    *,
    timeout_s: float,
    api_key: str | None,
) -> RequestResult:
    url = endpoint.rstrip("/") + "/v1/completions"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        url, data=canonical_json_bytes(body), headers=headers, method="POST"
    )
    started = time.perf_counter()
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout_s) as response:
            raw = response.read(MAX_HTTP_BYTES + 1)
            status = response.status
    except urllib.error.HTTPError as exc:
        detail = exc.read(2048).decode("utf-8", errors="replace")
        raise LabError(f"{url} returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LabError(f"request to {url} failed: {exc}") from exc
    latency_ms = (time.perf_counter() - started) * 1000
    if status < 200 or status >= 300:
        raise LabError(f"{url} returned HTTP {status}")
    if len(raw) > MAX_HTTP_BYTES:
        raise LabError(f"{url} response exceeds {MAX_HTTP_BYTES} bytes")
    parsed = _load_json_bytes(raw, f"response from {url}")
    choices, out_of_order = map_completion_response(parsed, len(body["prompt"]))
    digest_input = [
        {"index": row.index, "text_sha256": row.text_sha256, "tokens": row.token_count}
        for row in choices
    ]
    return RequestResult(
        latency_ms=latency_ms,
        choices=choices,
        output_sha256=sha256_json(digest_input),
        response_was_out_of_order=out_of_order,
    )


class NGramDraft:
    def __init__(self, context: int):
        self.context = context
        self.history: list[str] = []
        self.table: dict[tuple[str, ...], Counter[str]] = defaultdict(Counter)
        self.global_counts: Counter[str] = Counter()

    def observe(self, token: str) -> None:
        if len(self.history) >= self.context:
            self.table[tuple(self.history[-self.context :])][token] += 1
        self.global_counts[token] += 1
        self.history.append(token)

    def predict(self, width: int) -> list[str]:
        context = list(self.history[-self.context :])
        output: list[str] = []
        for _ in range(width):
            counts = self.table.get(tuple(context[-self.context :]))
            if counts:
                token = counts.most_common(1)[0][0]
            elif self.global_counts:
                token = self.global_counts.most_common(1)[0][0]
            else:
                token = ""
            output.append(token)
            context.append(token)
        return output


def replay_policy(
    traces: Sequence[Sequence[str]],
    *,
    width: int,
    context: int,
    prefix_fraction: float,
    evidence: str,
    source_digest: str,
) -> dict[str, Any]:
    if width < 1 or context < 1 or not 0 < prefix_fraction < 1:
        raise LabError("invalid policy replay parameters")
    usable = [list(trace) for trace in traces if len(trace) >= context + 2]
    if not usable:
        return {
            "status": "insufficient_trace",
            "width": width,
            "reason": f"no completion contains at least {context + 2} policy tokens",
        }

    windows = full_windows = repaired_windows = 0
    proposed_tokens = accepted_tokens = generated_tokens = 0
    exact = True
    started = time.perf_counter()
    for trace in usable:
        prefix = max(context + 1, int(len(trace) * prefix_fraction))
        prefix = min(prefix, len(trace) - 1)
        draft = NGramDraft(context)
        for token in trace[:prefix]:
            draft.observe(token)
        position = prefix
        rebuilt: list[str] = []
        while position < len(trace):
            count = min(width, len(trace) - position)
            proposal = draft.predict(count)
            truth = trace[position : position + count]
            accepted = 0
            for candidate, real in zip(proposal, truth):
                if candidate != real:
                    break
                accepted += 1
            windows += 1
            proposed_tokens += count
            accepted_tokens += accepted
            if accepted == count:
                full_windows += 1
                emitted = truth
            else:
                repaired_windows += 1
                emitted = truth[: accepted + 1]
            for token in emitted:
                draft.observe(token)
            rebuilt.extend(emitted)
            position += len(emitted)
        generated_tokens += len(trace) - prefix
        exact = exact and rebuilt == trace[prefix:]
    elapsed = max(time.perf_counter() - started, 0.000001)
    receipt = SpecReceipt(
        branch_id=f"openai-ngram-replay-w{width}",
        modality="token_policy_replay",
        units=windows,
        attempted_units=windows,
        accepted_units=full_windows,
        rejected_units=repaired_windows,
        repaired_units=repaired_windows,
        draft_s=0.0,
        verify_s=0.0,
        repair_s=0.0,
        overhead_s=elapsed,
        baseline_s=0.0,
        speculative_s=elapsed,
        speedup_x=0.0,
        exact=exact,
        quality_gate=exact,
        artifact_verified=False,
        baseline_source="absent",
        evidence=evidence,
        meta={
            "engine_neutral": True,
            "policy": "online_ngram_prompt_lookup",
            "context": context,
            "width": width,
            "trace_count": len(usable),
            "source_output_sha256": source_digest,
            "proposed_tokens": proposed_tokens,
            "accepted_tokens": accepted_tokens,
            "token_acceptance_fraction": (
                accepted_tokens / proposed_tokens if proposed_tokens else 0.0
            ),
            "generated_tokens": generated_tokens,
            "potential_target_call_reduction_x": (
                generated_tokens / windows if windows else 0.0
            ),
            "performance_claim": "none_policy_replay_only",
        },
    )
    return {
        "status": "ok",
        "width": width,
        "branch_action": decide_branch(receipt),
        "receipt": receipt.to_dict(),
    }


def load_prompts(path: str | None) -> list[str]:
    if path is None:
        return list(DEFAULT_PROMPTS)
    source = Path(path)
    raw = _read_small(source, MAX_REQUEST_BYTES, "prompt corpus")
    prompts: list[str] = []
    if source.suffix.lower() == ".json":
        value = _load_json_bytes(raw, "prompt corpus")
        if not isinstance(value, list) or not all(isinstance(row, str) for row in value):
            raise LabError(".json prompt corpus must be an array of strings")
        prompts = value
    else:
        for line_number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line, object_pairs_hook=_duplicate_safe_object)
            except (json.JSONDecodeError, LabError) as exc:
                raise LabError(f"prompt JSONL line {line_number}: {exc}") from exc
            if isinstance(value, str):
                prompt = value
            elif isinstance(value, dict) and set(value) == {"prompt"} and isinstance(value["prompt"], str):
                prompt = value["prompt"]
            else:
                raise LabError(
                    f"prompt JSONL line {line_number} must be a string or {{\"prompt\": string}}"
                )
            prompts.append(prompt)
    completion_request("validation-model", prompts, 1)
    return prompts


def _safe_endpoint(value: str, *, local_only: bool, allow_insecure_http: bool) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LabError("endpoint must be an absolute http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LabError("endpoint must not contain credentials, query, or fragment")
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if local_only and not loopback:
        raise LabError("local target endpoint must be loopback")
    if parsed.scheme == "http" and not loopback and not allow_insecure_http:
        raise LabError("non-loopback HTTP requires --allow-insecure-http")
    clean_path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, clean_path, "", "")
    )


def _load_runtime_lock(path: str) -> tuple[dict[str, Any], str]:
    source = Path(path)
    try:
        lock = runtime_lock_validator.load_lock(source)
        runtime_lock_validator.validate_lock(lock)
    except runtime_lock_validator.LockValidationError as exc:
        raise LabError(f"invalid vLLM runtime lock: {exc}") from exc
    return lock, hashlib.sha256(source.read_bytes()).hexdigest()


def resolve_target(args: argparse.Namespace, host: dict[str, Any]) -> dict[str, Any]:
    target = args.target
    if target not in TARGETS:
        raise LabError(f"unknown target {target!r}")
    if target == "embedded-mock":
        if args.endpoint or args.runtime_lock or args.model:
            raise LabError("embedded-mock owns its endpoint/model and accepts no runtime lock")
        variant = "baseline" if args.variant == "auto" else args.variant
        if variant != "baseline":
            raise LabError("embedded-mock is protocol-only and can only be labeled baseline")
        script_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        return {
            "class": target,
            "engine": "cx-openai-completions-contract-mock",
            "variant": variant,
            "model": "cx-vllm-contract-mock",
            "model_identity": {
                "revision": "synthetic-contract-v1",
                "weights_sha256": None,
                "tokenizer_sha256": None,
                "precision_id": "not_applicable",
                "complete": False,
            },
            "execution_site": "local_process",
            "accelerator_class": "none",
            "executor_identity_sha256": host["hardware_fingerprint_sha256"],
            "executor_identity_source": "local_host_probe",
            "runtime_identity_sha256": script_sha,
            "runtime_lock": None,
            "runtime_configuration_attested": True,
            "cuda": False,
            "non_cuda": True,
            "benchmark_domain": "protocol_control_path_only",
            "throughput_claim": "none",
        }

    if not args.endpoint:
        raise LabError(f"{target} requires --endpoint")
    endpoint = _safe_endpoint(
        args.endpoint,
        local_only=target != "remote-vllm-cuda",
        allow_insecure_http=args.allow_insecure_http,
    )
    if target == "remote-vllm-cuda":
        if not args.runtime_lock:
            raise LabError("remote-vllm-cuda requires --runtime-lock")
        lock, lock_sha = _load_runtime_lock(args.runtime_lock)
        locked_variant = lock["speculative_decoding"]["method"]
        variant = locked_variant if args.variant == "auto" else args.variant
        if variant != locked_variant:
            raise LabError(
                f"--variant {variant!r} contradicts runtime lock method {locked_variant!r}"
            )
        model = lock["model"]["served_model_name"]
        if args.model and args.model != model:
            raise LabError(f"--model {args.model!r} contradicts locked model {model!r}")
        executor_identity = sha256_text(args.executor_id) if args.executor_id else None
        return {
            "class": target,
            "engine": "vllm",
            "variant": variant,
            "model": model,
            "model_identity": {
                "revision": lock["model"]["revision"],
                "weights_sha256": lock["model"]["artifact_sha256"],
                # The current CUDA lock pins repository+revision rather than a
                # separately materialized tokenizer tree digest. Preserve that
                # distinction instead of pretending the revision is a file hash.
                "tokenizer_sha256": None,
                "tokenizer_repository": lock["model"]["tokenizer_repository"],
                "tokenizer_revision": lock["model"]["tokenizer_revision"],
                "precision_id": (
                    f"{lock['execution']['weight_format']}:"
                    f"{lock['execution']['quantization']}:"
                    f"{lock['execution']['dtype']}"
                ),
                "complete": True,
                "identity_source": "validated_cuda_runtime_lock",
            },
            "endpoint": endpoint,
            "execution_site": "remote_endpoint",
            "accelerator_class": "cuda",
            "executor_identity_sha256": executor_identity,
            "executor_identity_source": "operator_supplied" if executor_identity else "absent",
            "runtime_identity_sha256": lock_sha,
            "runtime_lock": {
                "path": str(Path(args.runtime_lock)),
                "sha256": lock_sha,
                "status": lock["status"],
                "vllm_version": lock["runtime"]["vllm_version"],
                "vllm_commit": lock["runtime"]["vllm_commit"],
                "container_image": lock["runtime"]["container_image"],
                "speculative_decoding": lock["speculative_decoding"],
            },
            "runtime_configuration_attested": True,
            "cuda": True,
            "non_cuda": False,
            "benchmark_domain": "remote_cuda_endpoint_e2e_including_transport",
            "throughput_claim": "remote_endpoint_e2e_not_kernel_only",
        }

    if args.runtime_lock:
        raise LabError(f"{target} cannot use the CUDA-specific runtime lock")
    if not args.model:
        raise LabError(f"{target} requires --model")
    weights_sha256 = _sha256_or_none(args.weights_sha256, "--weights-sha256")
    tokenizer_sha256 = _sha256_or_none(args.tokenizer_sha256, "--tokenizer-sha256")
    local_model_identity = {
        "revision": args.model_revision,
        "weights_sha256": weights_sha256,
        "tokenizer_sha256": tokenizer_sha256,
        "precision_id": args.precision_id,
        "complete": bool(
            args.model_revision and weights_sha256 and tokenizer_sha256 and args.precision_id
        ),
        "identity_source": "operator_supplied",
    }
    variant = "baseline" if args.variant == "auto" else args.variant
    if target == "local-vllm-metal":
        probe = inspect_vllm_metal(explicit_bin=args.vllm_metal_bin)
        detected_id = probe["installation"]["runtime_identity_sha256"]
        declared_id = sha256_text(args.runtime_id) if args.runtime_id else None
        runtime_id = declared_id or detected_id
        return {
            "class": target,
            "engine": "vllm-metal",
            "variant": variant,
            "model": args.model,
            "model_identity": local_model_identity,
            "endpoint": endpoint,
            "execution_site": "local_process",
            "accelerator_class": "metal",
            "executor_identity_sha256": host["hardware_fingerprint_sha256"],
            "executor_identity_source": "local_host_probe",
            "runtime_identity_sha256": runtime_id,
            "runtime_identity_source": (
                "operator_supplied" if declared_id else "installation_probe"
            ),
            "runtime_lock": None,
            "runtime_configuration_attested": False,
            "vllm_metal_probe": probe,
            "cuda": False,
            "non_cuda": True,
            "benchmark_domain": "local_metal_endpoint_e2e",
            "throughput_claim": "local_metal_endpoint_e2e_not_cuda",
        }

    if not args.runtime_id:
        raise LabError("local-openai-compatible requires --runtime-id")
    if args.accelerator_class not in {"metal", "cpu", "unknown"}:
        raise LabError("local-openai-compatible requires --accelerator-class")
    return {
        "class": target,
        "engine": args.engine_label or "openai-compatible",
        "variant": variant,
        "model": args.model,
        "model_identity": local_model_identity,
        "endpoint": endpoint,
        "execution_site": "local_process",
        "accelerator_class": args.accelerator_class,
        "executor_identity_sha256": host["hardware_fingerprint_sha256"],
        "executor_identity_source": "local_host_probe",
        "runtime_identity_sha256": sha256_text(args.runtime_id),
        "runtime_identity_source": "operator_supplied",
        "runtime_lock": None,
        "runtime_configuration_attested": False,
        "cuda": False,
        "non_cuda": True,
        "benchmark_domain": f"local_{args.accelerator_class}_endpoint_e2e",
        "throughput_claim": f"local_{args.accelerator_class}_endpoint_e2e_not_cuda",
    }


def _sample_record(index: int, result: RequestResult) -> dict[str, Any]:
    sources = sorted({choice.token_source for choice in result.choices})
    return {
        "sample_id": f"r{index + 1:04d}",
        "latency_ms": round(result.latency_ms, 6),
        "output_tokens": result.output_tokens,
        "output_sha256": result.output_sha256,
        "token_count_sources": sources,
        "response_choices_out_of_order": result.response_was_out_of_order,
        "completion_text_sha256": [choice.text_sha256 for choice in result.choices],
        "completion_token_counts": [choice.token_count for choice in result.choices],
    }


def _run_requests(
    endpoint: str,
    body: dict[str, Any],
    *,
    warmups: int,
    requests: int,
    concurrency: int,
    timeout_s: float,
    api_key: str | None,
) -> tuple[list[RequestResult], float]:
    for _ in range(warmups):
        request_completions(endpoint, body, timeout_s=timeout_s, api_key=api_key)

    def one(_index: int) -> RequestResult:
        return request_completions(endpoint, body, timeout_s=timeout_s, api_key=api_key)

    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(one, range(requests)))
    wall_s = time.perf_counter() - started
    return results, wall_s


def _performance_bridge(samples: list[dict[str, Any]]) -> dict[str, Any]:
    schema_sha = (
        hashlib.sha256(PERFORMANCE_SCHEMA.read_bytes()).hexdigest()
        if PERFORMANCE_SCHEMA.exists()
        else None
    )
    return {
        "canonical_manifest_schema": str(PERFORMANCE_SCHEMA.relative_to(REPO)),
        "canonical_manifest_schema_sha256": schema_sha,
        "status": "lab_capture_only_not_performance_proof_observation",
        "aligned_fields": [
            "fixed prompt batch",
            "output-token count",
            "per-sample end-to-end latency",
            "nearest-rank p95/p99",
            "output SHA-256",
        ],
        "sample_projection": [
            {
                "sample_id": row["sample_id"],
                "batch_size": len(row["completion_token_counts"]),
                "completed_output_tokens": row["output_tokens"],
                "endpoint_end_to_end_ms": row["latency_ms"],
                "output_sha256": row["output_sha256"],
            }
            for row in samples
        ],
        "missing_for_physical_proof": [
            "validated baseline/candidate manifests",
            "provisioning and queue timing boundaries",
            "power and starting thermal measurements",
            "competing-load snapshot",
            "fault and cleanup evidence",
        ],
    }


def build_capture(args: argparse.Namespace, endpoint_override: str | None = None) -> dict[str, Any]:
    host = host_identity()
    target = resolve_target(args, host)
    if endpoint_override:
        target["endpoint"] = endpoint_override
    endpoint = target.get("endpoint")
    if not endpoint:
        raise LabError("target endpoint was not resolved")
    prompts = load_prompts(args.prompts)
    model = target["model"]
    body = completion_request(model, prompts, args.max_tokens)
    workload = {
        "contract": "openai-v1-completions-batched-greedy-v1",
        "model": model,
        "model_identity": target["model_identity"],
        "prompts_sha256": sha256_json(prompts),
        "prompt_count": len(prompts),
        "max_tokens": args.max_tokens,
        "sampling": {
            "temperature": 0,
            "top_p": 1,
            "seed": 0,
            "n": 1,
            "logprobs": 0,
        },
        "batching": {
            "prompts_per_request": len(prompts),
            "concurrent_requests": args.concurrency,
        },
    }
    workload_digest = sha256_json(workload)
    api_key = os.environ.get(args.api_key_env) if args.api_key_env else None
    results, wall_s = _run_requests(
        endpoint,
        body,
        warmups=args.warmups,
        requests=args.requests,
        concurrency=args.concurrency,
        timeout_s=args.timeout,
        api_key=api_key,
    )
    samples = [_sample_record(index, result) for index, result in enumerate(results)]
    latencies = [result.latency_ms for result in results]
    total_tokens = sum(result.output_tokens for result in results)
    token_sources = sorted(
        {choice.token_source for result in results for choice in result.choices}
    )
    authoritative_tokens = bool(token_sources) and set(token_sources) <= {
        "logprobs.tokens",
        "usage.completion_tokens",
    }
    output_hashes = [result.output_sha256 for result in results]
    first = results[0]
    traces = [list(choice.policy_tokens) for choice in first.choices]
    source_digest = sha256_json(
        [[choice.text_sha256, choice.token_count] for choice in first.choices]
    )
    policy_evidence = "synthetic" if target["class"] == "embedded-mock" else "imported"
    policy = [
        replay_policy(
            traces,
            width=width,
            context=args.context,
            prefix_fraction=args.prefix_fraction,
            evidence=policy_evidence,
            source_digest=source_digest,
        )
        for width in args.widths
    ]
    summary = {
        "measured_wall_s": round(wall_s, 6),
        "measured_requests": args.requests,
        "concurrent_requests": args.concurrency,
        "total_output_tokens": total_tokens,
        "token_count_sources": token_sources,
        "token_count_authoritative": authoritative_tokens,
        "requests_per_second": round(args.requests / wall_s, 6),
        "output_tokens_per_second": (
            round(total_tokens / wall_s, 6) if authoritative_tokens else None
        ),
        "latency_ms": {
            "min": round(min(latencies), 6),
            "p50": round(percentile(latencies, 50), 6),
            "p95": round(percentile(latencies, 95), 6),
            "p99": round(percentile(latencies, 99), 6),
            "max": round(max(latencies), 6),
            "mean": round(statistics.fmean(latencies), 6),
        },
        "output_sha256": output_hashes[0],
        "all_output_sha256": sha256_json(output_hashes),
        "repeat_output_stable": len(set(output_hashes)) == 1,
        "response_reordering_exercised": any(
            result.response_was_out_of_order for result in results
        ),
    }
    pricing = None
    if args.hourly_cost_usd is not None:
        estimated = args.hourly_cost_usd * wall_s / 3600
        pricing = {
            "currency": "USD",
            "hourly_cost": args.hourly_cost_usd,
            "source": "operator_supplied_cli",
            "estimated_measured_window_cost": round(estimated, 10),
            "estimated_cost_per_million_output_tokens": (
                round(estimated * 1_000_000 / total_tokens, 8)
                if authoritative_tokens and total_tokens
                else None
            ),
            "notice": "No price was inferred by the harness.",
        }
    record = {
        "schema_version": 1,
        "record_kind": "cx_vllm_spec_lab_capture",
        "timestamp": now(),
        "status": "ok",
        "run_label": args.run_label,
        "comparison_group": args.comparison_group,
        "claim_scope": (
            "OpenAI-compatible endpoint E2E measurement plus engine-neutral policy replay; "
            "never a local CUDA or kernel-only claim"
        ),
        "target": target,
        "client_host": host,
        "workload": workload,
        "workload_sha256": workload_digest,
        "sampling": {
            "warmup_requests": args.warmups,
            "measured_requests": args.requests,
            "concurrency": args.concurrency,
            "timeout_s": args.timeout,
        },
        "authentication": {
            "api_key_env": args.api_key_env,
            "api_key_present": bool(api_key),
            "secret_recorded": False,
        },
        "measurement": summary,
        "samples": samples,
        "engine_neutral_policy_replays": policy,
        "pricing": pricing,
        "provenance": {
            "cuda": target["cuda"],
            "non_cuda": target["non_cuda"],
            "benchmark_domain": target["benchmark_domain"],
            "throughput_claim": target["throughput_claim"],
            "local_mac_result_may_be_labeled_cuda": False,
            "endpoint_runtime_self_attested": False,
        },
        "performance_proof_bridge": _performance_bridge(samples),
    }
    return record


def append_ledger(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(canonical_json_bytes(record).decode("utf-8") + "\n")


def write_artifact(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def load_capture(path: str) -> dict[str, Any]:
    value = _load_json_bytes(_read_small(Path(path), 32 << 20, "capture"), "capture")
    if not isinstance(value, dict):
        raise LabError("capture root must be an object")
    if value.get("schema_version") != 1 or value.get("record_kind") != "cx_vllm_spec_lab_capture":
        raise LabError(f"{path} is not a v1 CX vLLM lab capture")
    if value.get("status") != "ok":
        raise LabError(f"{path} is not a successful capture")
    required = {
        "run_label",
        "comparison_group",
        "target",
        "workload",
        "workload_sha256",
        "sampling",
        "measurement",
        "samples",
        "provenance",
    }
    missing = sorted(required - set(value))
    if missing:
        raise LabError(f"{path} capture is missing fields: {', '.join(missing)}")
    for field in ("target", "workload", "sampling", "measurement", "provenance"):
        if not isinstance(value[field], dict):
            raise LabError(f"{path} capture field {field} must be an object")
    target_required = {
        "class", "variant", "execution_site", "accelerator_class",
        "executor_identity_sha256", "runtime_identity_sha256",
    }
    if target_required - set(value["target"]):
        raise LabError(f"{path} capture target identity is incomplete")
    measurement_required = {
        "measured_wall_s", "output_tokens_per_second", "latency_ms"
    }
    if measurement_required - set(value["measurement"]):
        raise LabError(f"{path} capture measurement is incomplete")
    latency = value["measurement"]["latency_ms"]
    if not isinstance(latency, dict) or {"p50", "p95", "p99"} - set(latency):
        raise LabError(f"{path} capture latency summary is incomplete")
    if not isinstance(value["samples"], list) or not value["samples"]:
        raise LabError(f"{path} capture samples must be a non-empty array")
    for index, sample in enumerate(value["samples"]):
        if not isinstance(sample, dict) or not isinstance(sample.get("output_sha256"), str):
            raise LabError(f"{path} capture sample {index} is malformed")
    _sha256_or_none(value["workload_sha256"], "capture workload_sha256")
    return value


def _positive_ratio(numerator: Any, denominator: Any, label: str) -> float:
    for value, side in ((numerator, "numerator"), (denominator, "denominator")):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise LabError(f"{label} {side} must be finite and > 0")
    return numerator / denominator


def compare_captures(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    if baseline["comparison_group"] != candidate["comparison_group"]:
        reasons.append("comparison_group differs")
    if baseline["workload_sha256"] != candidate["workload_sha256"]:
        reasons.append("workload digest differs")
    if not baseline["workload"].get("model_identity", {}).get("complete"):
        reasons.append("baseline model identity is incomplete")
    if not candidate["workload"].get("model_identity", {}).get("complete"):
        reasons.append("candidate model identity is incomplete")
    if baseline["sampling"] != candidate["sampling"]:
        reasons.append("sampling or concurrency differs")
    for field in ("execution_site", "accelerator_class", "executor_identity_sha256"):
        if baseline["target"].get(field) != candidate["target"].get(field):
            reasons.append(f"executor {field} differs or is absent")
    if not baseline["target"].get("executor_identity_sha256"):
        reasons.append("baseline executor identity is absent")
    if not candidate["target"].get("executor_identity_sha256"):
        reasons.append("candidate executor identity is absent")
    if baseline["target"]["variant"] != "baseline":
        reasons.append("baseline capture is not labeled variant=baseline")
    if candidate["target"]["variant"] == "baseline":
        reasons.append("candidate capture is still labeled variant=baseline")
    if baseline["target"]["class"] == "embedded-mock" or candidate["target"]["class"] == "embedded-mock":
        reasons.append("embedded mock has no compute-performance evidence")
    if not baseline["target"].get("runtime_identity_sha256"):
        reasons.append("baseline runtime identity is absent")
    if not candidate["target"].get("runtime_identity_sha256"):
        reasons.append("candidate runtime identity is absent")

    lab_comparable = not reasons
    base_measurement = baseline["measurement"]
    candidate_measurement = candidate["measurement"]
    base_hashes = [row["output_sha256"] for row in baseline["samples"]]
    candidate_hashes = [row["output_sha256"] for row in candidate["samples"]]
    paired = min(len(base_hashes), len(candidate_hashes))
    output_match_rate = (
        sum(left == right for left, right in zip(base_hashes, candidate_hashes)) / paired
        if paired else 0.0
    )
    base_throughput = base_measurement.get("output_tokens_per_second")
    candidate_throughput = candidate_measurement.get("output_tokens_per_second")
    ratios = {
        "throughput_candidate_over_baseline": (
            round(_positive_ratio(candidate_throughput, base_throughput, "throughput"), 6)
            if base_throughput is not None and candidate_throughput is not None
            else None
        ),
        "p50_latency_candidate_over_baseline": round(
            _positive_ratio(
                candidate_measurement["latency_ms"]["p50"],
                base_measurement["latency_ms"]["p50"],
                "p50 latency",
            ),
            6,
        ),
        "p95_latency_candidate_over_baseline": round(
            _positive_ratio(
                candidate_measurement["latency_ms"]["p95"],
                base_measurement["latency_ms"]["p95"],
                "p95 latency",
            ),
            6,
        ),
        "p99_latency_candidate_over_baseline": round(
            _positive_ratio(
                candidate_measurement["latency_ms"]["p99"],
                base_measurement["latency_ms"]["p99"],
                "p99 latency",
            ),
            6,
        ),
        "wall_time_candidate_over_baseline": round(
            _positive_ratio(
                candidate_measurement["measured_wall_s"],
                base_measurement["measured_wall_s"],
                "wall time",
            ),
            6,
        ),
    }
    descriptive_ratio_valid = (
        lab_comparable
        and output_match_rate == 1.0
        and ratios["throughput_candidate_over_baseline"] is not None
    )
    cost_ratio = None
    if baseline.get("pricing") and candidate.get("pricing"):
        base_cost = baseline["pricing"]["estimated_measured_window_cost"]
        candidate_cost = candidate["pricing"]["estimated_measured_window_cost"]
        if base_cost > 0:
            cost_ratio = round(candidate_cost / base_cost, 6)
    return {
        "schema_version": 1,
        "record_kind": "cx_vllm_spec_lab_comparison",
        "timestamp": now(),
        "comparison_group": baseline["comparison_group"],
        "baseline_label": baseline["run_label"],
        "candidate_label": candidate["run_label"],
        "baseline_variant": baseline["target"]["variant"],
        "candidate_variant": candidate["target"]["variant"],
        "comparability": {
            "lab_comparable": lab_comparable,
            "reasons": reasons,
            "canonical_performance_proof_comparable": False,
            "canonical_reason": (
                "Lab captures are inputs to, not replacements for, validated physical "
                "manifests and observations."
            ),
            "same_non_cuda_substrate": (
                baseline["provenance"]["non_cuda"]
                and candidate["provenance"]["non_cuda"]
                and baseline["target"]["accelerator_class"]
                == candidate["target"]["accelerator_class"]
            ),
        },
        "correctness": {
            "paired_samples": paired,
            "exact_output_match_rate": round(output_match_rate, 6),
            "passed": output_match_rate == 1.0,
        },
        "ratios": ratios,
        "estimated_cost_candidate_over_baseline": cost_ratio,
        "descriptive_endpoint_ratio_valid": descriptive_ratio_valid,
        "speed_claim_valid": False,
        "speed_claim_blocker": (
            "Requires validated physical manifests/observations and review through "
            "scripts/performance_proof.py."
        ),
        "notice": (
            "Ratios are descriptive endpoint-E2E lab results. Publish a speed claim only "
            "after ingest through scripts/performance_proof.py."
        ),
    }


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be finite and > 0")
    return parsed


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be finite and >= 0")
    return parsed


def _environment_name(value: str) -> str:
    if not value or not (value[0].isalpha() or value[0] == "_"):
        raise argparse.ArgumentTypeError("must be an environment-variable name")
    if any(not (char.isalnum() or char == "_") for char in value):
        raise argparse.ArgumentTypeError("must be an environment-variable name")
    return value


def _lab_identifier(value: str) -> str:
    if not 1 <= len(value) <= 128:
        raise argparse.ArgumentTypeError("must contain 1..128 characters")
    if not value.isascii() or not value[0].isalnum() or any(
        not (char.isalnum() or char in "._-") for char in value
    ):
        raise argparse.ArgumentTypeError(
            "must start alphanumeric and contain only alphanumeric/._-"
        )
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser("inspect-metal", help="detect vLLM-Metal without installing it")
    inspect.add_argument("--vllm-metal-bin")

    serve = sub.add_parser("serve-mock", help="serve the strict local completions mock")
    serve.add_argument("--port", type=_nonnegative_int, default=8765)

    run = sub.add_parser("run", help="capture endpoint and engine-neutral policy measurements")
    run.add_argument("--target", choices=sorted(TARGETS), default="embedded-mock")
    run.add_argument("--endpoint")
    run.add_argument("--model")
    run.add_argument("--model-revision")
    run.add_argument("--weights-sha256")
    run.add_argument("--tokenizer-sha256")
    run.add_argument("--precision-id")
    run.add_argument("--runtime-lock")
    run.add_argument("--runtime-id")
    run.add_argument("--executor-id")
    run.add_argument("--engine-label")
    run.add_argument("--accelerator-class", choices=("metal", "cpu", "unknown"), default="unknown")
    run.add_argument("--variant", choices=sorted(VARIANTS), default="auto")
    run.add_argument("--vllm-metal-bin")
    run.add_argument("--prompts", help="JSON array or JSONL string/{prompt:string} corpus")
    run.add_argument("--max-tokens", type=_positive_int, default=64)
    run.add_argument("--warmups", type=_nonnegative_int, default=1)
    run.add_argument("--requests", type=_positive_int, default=5)
    run.add_argument("--concurrency", type=_positive_int, default=1)
    run.add_argument("--timeout", type=_positive_float, default=DEFAULT_TIMEOUT_S)
    run.add_argument("--widths", type=_positive_int, nargs="+", default=[1, 2, 4, 8])
    run.add_argument("--context", type=_positive_int, default=4)
    run.add_argument("--prefix-fraction", type=float, default=0.25)
    run.add_argument("--run-label", type=_lab_identifier, default="smoke")
    run.add_argument(
        "--comparison-group", type=_lab_identifier, default="local-vllm-spec-lab-v1"
    )
    run.add_argument("--api-key-env", type=_environment_name, default="CX_VLLM_API_KEY")
    run.add_argument("--allow-insecure-http", action="store_true")
    run.add_argument("--hourly-cost-usd", type=_nonnegative_float)
    run.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    run.add_argument("--no-ledger", action="store_true")
    run.add_argument("--output", type=Path)

    compare = sub.add_parser("compare", help="compare sequential baseline/candidate captures")
    compare.add_argument("--baseline", required=True)
    compare.add_argument("--candidate", required=True)
    compare.add_argument("--output", type=Path)
    return parser


def _validate_run_args(args: argparse.Namespace) -> None:
    if args.max_tokens > 4096:
        raise LabError("--max-tokens must be <= 4096")
    if args.concurrency > args.requests:
        raise LabError("--concurrency cannot exceed --requests")
    if args.requests > 10_000:
        raise LabError("--requests must be <= 10000")
    if args.warmups > 1_000:
        raise LabError("--warmups must be <= 1000")
    if args.concurrency > 256:
        raise LabError("--concurrency must be <= 256")
    if args.timeout > 3_600:
        raise LabError("--timeout must be <= 3600 seconds")
    if not 0 < args.prefix_fraction < 1 or not math.isfinite(args.prefix_fraction):
        raise LabError("--prefix-fraction must be finite and in (0,1)")
    args.widths = sorted(set(args.widths))
    if len(args.widths) > 16 or max(args.widths) > 64:
        raise LabError("--widths must contain at most 16 unique values, each <= 64")
    if args.context > 128:
        raise LabError("--context must be <= 128")
    for label, value in (
        ("--model", args.model),
        ("--model-revision", args.model_revision),
        ("--precision-id", args.precision_id),
        ("--runtime-id", args.runtime_id),
        ("--executor-id", args.executor_id),
        ("--engine-label", args.engine_label),
    ):
        if value is not None and not 1 <= len(value) <= 4096:
            raise LabError(f"{label} must contain 1..4096 characters")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inspect-metal":
            print(json.dumps(inspect_vllm_metal(explicit_bin=args.vllm_metal_bin), sort_keys=True, indent=2))
            return 0
        if args.command == "serve-mock":
            with EmbeddedMock(port=args.port) as mock:
                print(json.dumps({"status": "ready", "endpoint": mock.endpoint}), flush=True)
                try:
                    while True:
                        time.sleep(1)
                except KeyboardInterrupt:
                    return 0
        if args.command == "compare":
            result = compare_captures(load_capture(args.baseline), load_capture(args.candidate))
            if args.output:
                write_artifact(args.output, result)
            print(json.dumps(result, sort_keys=True, indent=2))
            return 0 if result["descriptive_endpoint_ratio_valid"] else 2
        if args.command == "run":
            _validate_run_args(args)
            if args.target == "embedded-mock":
                with EmbeddedMock() as mock:
                    record = build_capture(args, endpoint_override=mock.endpoint)
            else:
                record = build_capture(args)
            if not args.no_ledger:
                append_ledger(args.ledger, record)
            if args.output:
                write_artifact(args.output, record)
            print(json.dumps(record, sort_keys=True, indent=2))
            return 0
        raise LabError(f"unhandled command {args.command!r}")
    except LabError as exc:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "record_kind": "cx_vllm_spec_lab_error",
                    "status": "error",
                    "error": str(exc),
                },
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
