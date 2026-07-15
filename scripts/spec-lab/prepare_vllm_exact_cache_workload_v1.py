#!/usr/bin/env python3
"""Prepare, but never run, a pinned vLLM-Metal exact-cache ABBA workload.

This CPU-only helper turns a supplied pre-tokenized source contract into the
files consumed by ``cx_inference_exact_cache_runner_v1.py``:

* a real OpenAI-completions workload that always asks for ``token_ids``;
* a retained, target-only/no-prefix/no-speculative vLLM launch plan; and
* identical baseline/candidate runtime identity files.

It never imports vLLM, launches a process, opens an endpoint, loads model
weights, or measures time.  Input token IDs are deliberately supplied by the
operator rather than inferred from text: the exact tokenizer must create them
on the Mac that will run the pinned endpoint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
from typing import Any, Mapping, Sequence
import urllib.parse


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import cx_inference_exact_cache_runner_v1 as exact_cache  # noqa: E402
import cx_inference_policy_v1 as policy  # noqa: E402
import screen_inference_lane_abba as abba  # noqa: E402


SCHEMA_VERSION = 1
SOURCE_KIND = "cx_vllm_exact_cache_prep_source_v1"
LAUNCH_PLAN_KIND = "cx_vllm_exact_cache_launch_plan_v1"
RUNTIME_INPUT_KIND = "cx_vllm_exact_cache_runtime_identity_input_v1"
MAX_SOURCE_BYTES = 4 << 20
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REVISION_RE = re.compile(r"^[0-9a-f]{40,64}$")
ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
SKIP_TREE_PARTS = frozenset({".git", ".artifacts", "__pycache__"})


class PreparationError(ValueError):
    """The source cannot make a safely bound future endpoint workload."""


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise PreparationError("canonical_json_failed") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def _duplicate_safe_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PreparationError(f"duplicate_json_key_{key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise PreparationError(f"nonfinite_json_constant_{value}")


def _read_source(path_value: Path) -> dict[str, Any]:
    if not path_value.is_absolute() or path_value.is_symlink() or not path_value.is_file():
        raise PreparationError("source_path_unsafe")
    raw = path_value.read_bytes()
    if not raw or len(raw) > MAX_SOURCE_BYTES:
        raise PreparationError("source_size_invalid")
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_duplicate_safe_object, parse_constant=_reject_constant
        )
    except (UnicodeDecodeError, json.JSONDecodeError, PreparationError) as exc:
        raise PreparationError("source_invalid_json") from exc
    if not isinstance(value, dict):
        raise PreparationError("source_root_must_be_object")
    return value


def _exact(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PreparationError(f"{label}_must_be_object")
    missing = sorted(fields - set(value))
    unknown = sorted(set(value) - fields)
    if missing or unknown:
        raise PreparationError(
            f"{label}_fields_invalid_missing_{','.join(missing) or 'none'}_unknown_{','.join(unknown) or 'none'}"
        )
    return value


def _string(value: Any, label: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise PreparationError(f"{label}_must_be_nonempty_string")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise PreparationError(f"{label}_must_be_lowercase_sha256")
    return value


def _revision(value: Any, label: str) -> str:
    if not isinstance(value, str) or not REVISION_RE.fullmatch(value):
        raise PreparationError(f"{label}_must_be_full_revision")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0, maximum: int = 1_000_000) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise PreparationError(f"{label}_must_be_integer_in_range")
    return value


def _new_absolute_path(path_value: Path, label: str) -> Path:
    if not path_value.is_absolute() or path_value.exists() or path_value.is_symlink() or not path_value.parent.is_dir():
        raise PreparationError(f"{label}_must_be_new_absolute_path")
    return path_value


def _write_new(path: Path, value: Any, label: str) -> None:
    payload = canonical_json_bytes(value) + b"\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        raise PreparationError(f"{label}_write_failed") from exc
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise PreparationError(f"{label}_short_write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _tokens(value: Any, label: str) -> list[int]:
    if not isinstance(value, list) or not value:
        raise PreparationError(f"{label}_must_be_nonempty_token_id_list")
    return [_integer(token, f"{label}_{index}", maximum=(1 << 32) - 1) for index, token in enumerate(value)]


def _flag_value(argv: Sequence[str], flag: str) -> str | None:
    positions = [index for index, item in enumerate(argv) if item == flag]
    if len(positions) > 1:
        raise PreparationError(f"launch_{flag.removeprefix('--')}_repeated")
    if not positions:
        return None
    index = positions[0]
    if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
        raise PreparationError(f"launch_{flag.removeprefix('--')}_missing_value")
    return argv[index + 1]


def _parse_endpoint(value: Any) -> dict[str, Any]:
    raw = _exact(value, {"url", "timeout_secs", "authorization_env"}, "endpoint")
    url = _string(raw["url"], "endpoint.url")
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != "/v1/completions"
    ):
        raise PreparationError("endpoint_must_be_clean_completions_url")
    if type(raw["timeout_secs"]) not in {int, float} or not 0.001 <= float(raw["timeout_secs"]) <= 7200:
        raise PreparationError("endpoint_timeout_invalid")
    auth = raw["authorization_env"]
    if auth is not None and (not isinstance(auth, str) or not ENV_RE.fullmatch(auth)):
        raise PreparationError("endpoint_authorization_env_invalid")
    return {"url": url, "timeout_secs": float(raw["timeout_secs"]), "authorization_env": auth}


def _parse_model(value: Any) -> dict[str, str]:
    raw = _exact(
        value,
        {
            "model_id",
            "model_revision",
            "tokenizer_id",
            "tokenizer_revision",
            "weights_sha256",
            "tokenizer_sha256",
            "precision_id",
        },
        "model",
    )
    return {
        "model_id": _string(raw["model_id"], "model.model_id"),
        "model_revision": _revision(raw["model_revision"], "model.model_revision"),
        "tokenizer_id": _string(raw["tokenizer_id"], "model.tokenizer_id"),
        "tokenizer_revision": _revision(raw["tokenizer_revision"], "model.tokenizer_revision"),
        "weights_sha256": _sha(raw["weights_sha256"], "model.weights_sha256"),
        "tokenizer_sha256": _sha(raw["tokenizer_sha256"], "model.tokenizer_sha256"),
        "precision_id": _string(raw["precision_id"], "model.precision_id", maximum=128),
    }


def _parse_launch(value: Any, *, model: Mapping[str, str], endpoint: Mapping[str, Any], max_total_tokens: int, concurrency: int) -> dict[str, Any]:
    raw = _exact(value, {"argv", "environment"}, "launch")
    argv_raw = raw["argv"]
    if not isinstance(argv_raw, list) or len(argv_raw) < 3:
        raise PreparationError("launch_argv_invalid")
    argv = [_string(item, f"launch.argv_{index}", maximum=8192) for index, item in enumerate(argv_raw)]
    if not Path(argv[0]).is_absolute() or argv[1] != "serve":
        raise PreparationError("launch_must_be_absolute_vllm_serve_command")
    forbidden = {"--speculative-config", "--enable-prefix-caching", "--async-scheduling"}
    if any(flag in argv for flag in forbidden) or "--no-enable-prefix-caching" not in argv or "--no-async-scheduling" not in argv:
        raise PreparationError("launch_must_disable_prefix_cache_async_and_speculation")
    if _flag_value(argv, "--served-model-name") != model["model_id"]:
        raise PreparationError("launch_served_model_name_mismatch")
    revision = _flag_value(argv, "--revision")
    if revision is not None and revision != model["model_revision"]:
        raise PreparationError("launch_revision_mismatch")
    parsed = urllib.parse.urlsplit(str(endpoint["url"]))
    if _flag_value(argv, "--host") != parsed.hostname or _flag_value(argv, "--port") != str(parsed.port or 80):
        raise PreparationError("launch_endpoint_host_or_port_mismatch")
    for flag, required in (("--max-model-len", max_total_tokens), ("--max-num-batched-tokens", max_total_tokens), ("--max-num-seqs", concurrency)):
        raw_value = _flag_value(argv, flag)
        try:
            actual = int(raw_value) if raw_value is not None else 0
        except ValueError as exc:
            raise PreparationError(f"launch_{flag.removeprefix('--')}_invalid") from exc
        if actual < required:
            raise PreparationError(f"launch_{flag.removeprefix('--')}_too_small")
    environment_raw = raw["environment"]
    if not isinstance(environment_raw, dict):
        raise PreparationError("launch_environment_must_be_object")
    environment: dict[str, str] = {}
    for name, setting in environment_raw.items():
        if not isinstance(name, str) or not ENV_RE.fullmatch(name) or not isinstance(setting, str):
            raise PreparationError("launch_environment_invalid")
        if any(word in name for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "AUTH")):
            raise PreparationError("launch_environment_must_not_retain_secrets")
        environment[name] = setting
    if environment.get("VLLM_METAL_USE_PAGED_ATTENTION") != "1" or environment.get("VLLM_ENABLE_V1_MULTIPROCESSING") != "0":
        raise PreparationError("launch_metal_required_environment_missing")
    return {"argv": argv, "environment": dict(sorted(environment.items()))}


def _parse_source(value: Mapping[str, Any]) -> dict[str, Any]:
    raw = _exact(value, {"schema_version", "kind", "model", "endpoint", "workload", "launch"}, "source")
    if raw["schema_version"] != SCHEMA_VERSION or raw["kind"] != SOURCE_KIND:
        raise PreparationError("source_identity_invalid")
    model = _parse_model(raw["model"])
    endpoint = _parse_endpoint(raw["endpoint"])
    workload_raw = _exact(raw["workload"], {"tenant_scope_sha256", "sampling", "max_output_tokens", "concurrency", "requests"}, "source.workload")
    tenant = _sha(workload_raw["tenant_scope_sha256"], "source.workload.tenant_scope_sha256")
    sampling_raw = _exact(workload_raw["sampling"], {"temperature", "top_p", "seed", "n"}, "source.workload.sampling")
    if sampling_raw["temperature"] != 0 or sampling_raw["top_p"] != 1 or sampling_raw["n"] != 1:
        raise PreparationError("source_workload_must_be_greedy_single_completion")
    sampling = {"temperature": 0, "top_p": 1, "seed": _integer(sampling_raw["seed"], "source.workload.sampling.seed"), "n": 1}
    max_output = _integer(workload_raw["max_output_tokens"], "source.workload.max_output_tokens", minimum=1, maximum=32768)
    concurrency = _integer(workload_raw["concurrency"], "source.workload.concurrency", minimum=1, maximum=4096)
    rows_raw = workload_raw["requests"]
    if not isinstance(rows_raw, list) or not 1 <= len(rows_raw) <= 4096:
        raise PreparationError("source_workload_requests_invalid")
    requests: list[dict[str, Any]] = []
    for index, item in enumerate(rows_raw):
        row = _exact(item, {"prompt", "input_token_ids", "response_reuse_authorized"}, f"source.workload.requests_{index}")
        if row["response_reuse_authorized"] is not True:
            raise PreparationError("source_every_request_must_authorize_exact_reuse")
        requests.append({"prompt": _string(row["prompt"], f"source.workload.requests_{index}.prompt", maximum=1 << 20), "input_token_ids": _tokens(row["input_token_ids"], f"source.workload.requests_{index}.input_token_ids")})
    max_total = max(len(row["input_token_ids"]) + max_output for row in requests)
    launch = _parse_launch(raw["launch"], model=model, endpoint=endpoint, max_total_tokens=max_total, concurrency=concurrency)
    return {"model": model, "endpoint": endpoint, "workload": {"tenant_scope_sha256": tenant, "sampling": sampling, "max_output_tokens": max_output, "concurrency": concurrency, "requests": requests}, "launch": launch}


def _digest_tree(root_value: Path, label: str) -> dict[str, Any]:
    root = root_value.resolve(strict=True)
    if root_value.is_symlink() or not root.is_dir():
        raise PreparationError(f"{label}_root_unsafe")
    files: list[dict[str, str]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if any(part in SKIP_TREE_PARTS or part.startswith(".venv") for part in relative.parts):
            continue
        if path.is_symlink():
            raise PreparationError(f"{label}_tree_contains_symlink")
        if path.is_dir():
            continue
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise PreparationError(f"{label}_tree_contains_nonregular_file")
        before = (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        payload = path.read_bytes()
        after_info = path.stat()
        if before != (after_info.st_size, after_info.st_mtime_ns, after_info.st_ctime_ns):
            raise PreparationError(f"{label}_tree_changed_while_hashing")
        files.append({"path": relative.as_posix(), "sha256": sha256_bytes(payload)})
    if not files:
        raise PreparationError(f"{label}_tree_empty")
    return {"path": str(root), "file_count": len(files), "tree_sha256": sha256_json({"contract": "cx-tree-sha256-v1", "files": files})}


def _git_commit(engine_root: Path) -> str:
    try:
        result = subprocess.run(["git", "-C", str(engine_root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise PreparationError("engine_root_git_commit_unavailable") from exc
    commit = result.stdout.strip()
    if result.returncode != 0 or not REVISION_RE.fullmatch(commit):
        raise PreparationError("engine_root_git_commit_unavailable")
    return commit


def _sysctl(name: str) -> str | None:
    try:
        result = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value or None


def capture_host() -> dict[str, Any]:
    host = {
        "system": platform.system(),
        "machine": platform.machine(),
        "hardware_model": _sysctl("hw.model") or "unresolved",
        "cpu_brand": _sysctl("machdep.cpu.brand_string") or platform.processor() or "unresolved",
        "memory_bytes": _sysctl("hw.memsize") or "unresolved",
    }
    if host["system"] != "Darwin" or host["machine"] != "arm64":
        raise PreparationError("target_must_be_apple_silicon_macos")
    return host


def _build_workload(source: Mapping[str, Any], runtime_sha256: str) -> dict[str, Any]:
    model = source["model"]
    work = source["workload"]
    requests: list[dict[str, Any]] = []
    for index, row in enumerate(work["requests"]):
        body = {"model": model["model_id"], "prompt": row["prompt"], "max_tokens": work["max_output_tokens"], **work["sampling"], "return_token_ids": True, "stream": False}
        request: dict[str, Any] = {
            "contract": policy.REQUEST_CONTRACT,
            "request_identity_sha256": "",
            "tenant_scope_sha256": work["tenant_scope_sha256"],
            "model_id": model["model_id"],
            "model_revision": model["model_revision"],
            "tokenizer_sha256": model["tokenizer_sha256"],
            "runtime_sha256": runtime_sha256,
            "sampling_contract_sha256": sha256_json(work["sampling"]),
            "input_token_ids_sha256": sha256_json(row["input_token_ids"]),
            "shared_prefix_token_ids_sha256": None,
            "shared_prefix_token_count": 0,
            "max_output_tokens": work["max_output_tokens"],
            "concurrency": work["concurrency"],
            "intent": policy.INTENT_EXPERIMENTAL,
            "response_reuse_authorized": True,
            "prefix_reuse_authorized": False,
        }
        request["request_identity_sha256"] = policy.request_identity_sha256(request)
        requests.append({"request_index": index, "request": request, "input_token_ids": row["input_token_ids"], "completion_request": body})
    digests = [exact_cache._request_digest(row) for row in requests]
    logical = {
        "model": {"model_id": model["model_id"], "model_revision": model["model_revision"], "tokenizer_id": model["tokenizer_id"], "tokenizer_revision": model["tokenizer_revision"]},
        "corpus_sha256": sha256_json([{"request": row["request"], "input_token_ids": row["input_token_ids"], "completion_request": row["completion_request"]} for row in requests]),
        "request_digests": digests,
        "request_order_sha256": sha256_json(digests),
        "input_token_ids_sha256": sha256_json([row["input_token_ids"] for row in requests]),
        "sampling_contract_sha256": sha256_json(work["sampling"]),
        "sampling": work["sampling"],
        "max_output_tokens": work["max_output_tokens"],
        "concurrency": work["concurrency"],
        "reuse_contract": {"eligible_request_indexes": list(range(len(requests))), "exact_request_key_schema_sha256": exact_cache.EXACT_REQUEST_KEY_SCHEMA_SHA256, "shared_prefix_token_ids_sha256": None, "shared_prefix_token_count": 0, "required_eligible_hit_rate": 1.0},
    }
    return {"schema_version": exact_cache.SCHEMA_VERSION, "kind": exact_cache.WORKLOAD_KIND, "logical_work": logical, "endpoint": source["endpoint"], "requests": requests}


def prepare(*, source_path: Path, engine_root: Path, vllm_core_root: Path, metal_runtime_root: Path, workload_out: Path, launch_plan_out: Path, runtime_input_out: Path, baseline_runtime_out: Path, candidate_runtime_out: Path, host: Mapping[str, Any] | None = None) -> dict[str, Any]:
    outputs = [workload_out, launch_plan_out, runtime_input_out, baseline_runtime_out, candidate_runtime_out]
    if len(set(outputs)) != len(outputs):
        raise PreparationError("output_paths_must_differ")
    paths = [_new_absolute_path(item, label) for item, label in zip(outputs, ("workload_out", "launch_plan_out", "runtime_input_out", "baseline_runtime_out", "candidate_runtime_out"), strict=True)]
    source = _parse_source(_read_source(source_path))
    source_sha = sha256_json(source)
    host_value = dict(host) if host is not None else capture_host()
    if host_value.get("system") != "Darwin" or host_value.get("machine") != "arm64":
        raise PreparationError("target_must_be_apple_silicon_macos")
    engine = _digest_tree(engine_root, "engine")
    engine["engine_commit"] = _git_commit(engine_root.resolve(strict=True))
    core = _digest_tree(vllm_core_root, "vllm_core")
    metal = _digest_tree(metal_runtime_root, "metal_runtime")
    launch_plan = {"schema_version": SCHEMA_VERSION, "kind": LAUNCH_PLAN_KIND, "source_sha256": source_sha, "endpoint": source["endpoint"], "model": {key: source["model"][key] for key in ("model_id", "model_revision", "precision_id")}, "launch": source["launch"], "lane": {"name": "exact_request_reuse", "baseline": "target_only_direct_decode", "candidate": "cx_exact_response_cache_then_direct_target_on_miss", "prefix_cache_enabled": False, "speculative_decode_enabled": False, "stream": False, "return_token_ids": True}}
    resolved_config_sha = sha256_json(launch_plan)
    runtime_input = {"schema_version": SCHEMA_VERSION, "kind": RUNTIME_INPUT_KIND, "source_sha256": source_sha, "host": host_value, "engine": engine, "vllm_core": core, "metal_runtime": metal, "resolved_engine_config_sha256": resolved_config_sha, "model": source["model"]}
    runtime_sha = sha256_json(runtime_input)
    runtime = {"backend": "metal", "host_hardware_sha256": sha256_json(host_value), "core_sha256": core["tree_sha256"], "runtime_sha256": runtime_sha, "resolved_engine_config_sha256": resolved_config_sha, "engine_id": "vllm-metal-cx", "engine_commit": engine["engine_commit"], "metal_runtime_sha256": metal["tree_sha256"], "weights_sha256": source["model"]["weights_sha256"], "tokenizer_sha256": source["model"]["tokenizer_sha256"], "precision_id": source["model"]["precision_id"]}
    try:
        abba._parse_runtime(runtime)
    except abba.InferenceLaneContractError as exc:  # pragma: no cover - defensive schema lock.
        raise PreparationError("generated_runtime_invalid") from exc
    workload = _build_workload(source, runtime_sha)
    try:
        abba._parse_logical_work(workload["logical_work"], "exact_request_reuse")
        for row in workload["requests"]:
            policy._validate_request(row["request"])
    except (abba.InferenceLaneContractError, policy.PolicyInputError) as exc:  # pragma: no cover - defensive schema lock.
        raise PreparationError("generated_workload_invalid") from exc
    for path, value, label in zip(paths, (workload, launch_plan, runtime_input, runtime, runtime), ("workload_out", "launch_plan_out", "runtime_input_out", "baseline_runtime_out", "candidate_runtime_out"), strict=True):
        _write_new(path, value, label)
    return {"status": "prepared_unmeasured", "workload": str(workload_out), "launch_plan": str(launch_plan_out), "runtime_input": str(runtime_input_out), "baseline_runtime": str(baseline_runtime_out), "candidate_runtime": str(candidate_runtime_out), "runtime_sha256": runtime_sha, "claim": "CPU-only preparation; no vLLM server, target request, cache prime, ABBA trial, speed, or authorization claim"}


def status() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "kind": "cx_vllm_exact_cache_prep_status_v1", "measurement_status": "unmeasured", "required_endpoint": {"path": "/v1/completions", "stream": False, "request_field": "return_token_ids: true", "response_field": "choices[0].token_ids"}, "required_launch": ["absolute vllm serve command", "--no-enable-prefix-caching", "--no-async-scheduling", "no --speculative-config", "VLLM_METAL_USE_PAGED_ATTENTION=1", "VLLM_ENABLE_V1_MULTIPROCESSING=0"], "claim": "no server started and no performance claim"}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    command = sub.add_parser("prepare")
    command.add_argument("--source", type=Path, required=True)
    command.add_argument("--engine-root", type=Path, required=True)
    command.add_argument("--vllm-core-root", type=Path, required=True)
    command.add_argument("--metal-runtime-root", type=Path, required=True)
    command.add_argument("--workload-out", type=Path, required=True)
    command.add_argument("--launch-plan-out", type=Path, required=True)
    command.add_argument("--runtime-input-out", type=Path, required=True)
    command.add_argument("--baseline-runtime-out", type=Path, required=True)
    command.add_argument("--candidate-runtime-out", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = status() if args.command == "status" else prepare(source_path=args.source, engine_root=args.engine_root, vllm_core_root=args.vllm_core_root, metal_runtime_root=args.metal_runtime_root, workload_out=args.workload_out, launch_plan_out=args.launch_plan_out, runtime_input_out=args.runtime_input_out, baseline_runtime_out=args.baseline_runtime_out, candidate_runtime_out=args.candidate_runtime_out)
    except PreparationError as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
