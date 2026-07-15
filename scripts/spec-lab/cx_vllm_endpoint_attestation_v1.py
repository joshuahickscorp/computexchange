#!/usr/bin/env python3
"""Create and validate a local vLLM endpoint identity attestation.

This is deliberately a *local-attestation* contract, not an independent
security proof.  It closes the easy measurement gap where a pinned workload
could otherwise point at an arbitrary HTTP server: it binds a prepared launch
plan, the observed live process command, a sanitized launch environment, the
served-model response, a frozen startup-log snapshot, and rehashed engine/core
/Metal trees before an exact-cache arm is allowed to call the endpoint.

The server is started outside this helper.  That keeps server lifetime and GPU
resource ownership explicit.  ``attest`` only observes an already-running
loopback/approved endpoint and writes new immutable files.  The resulting
artifact is marked local-unattested and cannot turn on product authorization.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
from typing import Any, Callable, Mapping, Sequence
import urllib.error
import urllib.parse
import urllib.request


SCHEMA_VERSION = 1
KIND = "cx_vllm_endpoint_attestation_v1"
LAUNCH_PLAN_KIND = "cx_vllm_exact_cache_launch_plan_v1"
RUNTIME_INPUT_KIND = "cx_vllm_exact_cache_runtime_identity_input_v1"
MAX_JSON_BYTES = 8 << 20
MAX_LOG_BYTES = 64 << 20
MAX_HTTP_BYTES = 8 << 20
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
SKIP_TREE_PARTS = frozenset({".git", ".artifacts", "__pycache__"})


class EndpointAttestationError(ValueError):
    """An endpoint cannot be tied safely to the prepared local runtime."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise EndpointAttestationError("canonical_json_failed") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def _duplicate_safe_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise EndpointAttestationError(f"duplicate_json_key_{key}")
        output[key] = value
    return output


def _reject_constant(value: str) -> None:
    raise EndpointAttestationError(f"nonfinite_json_constant_{value}")


def _exact(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EndpointAttestationError(f"{label}_must_be_object")
    missing = sorted(fields - set(value))
    unknown = sorted(set(value) - fields)
    if missing or unknown:
        raise EndpointAttestationError(
            f"{label}_fields_invalid_missing_{','.join(missing) or 'none'}"
            f"_unknown_{','.join(unknown) or 'none'}"
        )
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise EndpointAttestationError(f"{label}_must_be_lowercase_sha256")
    return value


def _string(value: Any, label: str, *, maximum: int = 16_384) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise EndpointAttestationError(f"{label}_must_be_nonempty_string")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise EndpointAttestationError(f"{label}_must_be_integer_in_range")
    return value


def _stable(value: Any) -> Any:
    return json.loads(canonical_json_bytes(value).decode("utf-8"))


def _safe_existing(path_value: Path | str, label: str, *, maximum: int) -> tuple[Path, bytes]:
    path = Path(path_value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise EndpointAttestationError(f"{label}_path_unsafe")
    try:
        before = path.stat()
        raw = path.read_bytes()
        after = path.stat()
    except OSError as exc:
        raise EndpointAttestationError(f"{label}_unreadable") from exc
    identity_before = (before.st_dev, before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if not stat.S_ISREG(before.st_mode) or before.st_size > maximum or identity_before != identity_after:
        raise EndpointAttestationError(f"{label}_changed_or_invalid_size")
    return path, raw


def _read_json(path_value: Path | str, label: str) -> tuple[Path, dict[str, Any], bytes]:
    path, raw = _safe_existing(path_value, label, maximum=MAX_JSON_BYTES)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_duplicate_safe_object, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, EndpointAttestationError) as exc:
        raise EndpointAttestationError(f"{label}_invalid_json") from exc
    if not isinstance(value, dict):
        raise EndpointAttestationError(f"{label}_root_must_be_object")
    return path, value, raw


def _new_path(path_value: Path | str, label: str) -> Path:
    path = Path(path_value)
    if not path.is_absolute() or path.exists() or path.is_symlink() or path.parent.is_symlink() or not path.parent.is_dir():
        raise EndpointAttestationError(f"{label}_must_be_new_absolute_path")
    return path


def _write_new(path: Path, payload: bytes, label: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise EndpointAttestationError(f"{label}_write_failed") from exc
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise EndpointAttestationError(f"{label}_short_write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _parse_endpoint(value: Any, label: str) -> dict[str, Any]:
    raw = _exact(value, {"url", "timeout_secs", "authorization_env"}, label)
    url = _string(raw["url"], f"{label}.url")
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
        raise EndpointAttestationError(f"{label}_must_be_clean_completions_url")
    timeout = raw["timeout_secs"]
    if type(timeout) not in (int, float) or not 0.001 <= float(timeout) <= 7200:
        raise EndpointAttestationError(f"{label}_timeout_invalid")
    auth = raw["authorization_env"]
    if auth is not None and (not isinstance(auth, str) or not ENV_RE.fullmatch(auth)):
        raise EndpointAttestationError(f"{label}_authorization_env_invalid")
    return {"url": url, "timeout_secs": float(timeout), "authorization_env": auth}


def _parse_launch_plan(value: Any) -> dict[str, Any]:
    raw = _exact(value, {"schema_version", "kind", "source_sha256", "endpoint", "model", "launch", "lane"}, "launch_plan")
    if raw["schema_version"] != SCHEMA_VERSION or raw["kind"] != LAUNCH_PLAN_KIND:
        raise EndpointAttestationError("launch_plan_identity_invalid")
    source_sha = _sha(raw["source_sha256"], "launch_plan.source_sha256")
    endpoint = _parse_endpoint(raw["endpoint"], "launch_plan.endpoint")
    model = _exact(raw["model"], {"model_id", "model_revision", "precision_id"}, "launch_plan.model")
    model_id = _string(model["model_id"], "launch_plan.model.model_id")
    model_revision = _string(model["model_revision"], "launch_plan.model.model_revision", maximum=128)
    precision_id = _string(model["precision_id"], "launch_plan.model.precision_id", maximum=128)
    launch = _exact(raw["launch"], {"argv", "environment"}, "launch_plan.launch")
    if not isinstance(launch["argv"], list) or len(launch["argv"]) < 3:
        raise EndpointAttestationError("launch_plan.argv_invalid")
    argv = [_string(item, f"launch_plan.argv_{index}") for index, item in enumerate(launch["argv"])]
    if not Path(argv[0]).is_absolute() or argv[1] != "serve":
        raise EndpointAttestationError("launch_plan.argv_not_absolute_vllm_serve")
    environment_raw = launch["environment"]
    if not isinstance(environment_raw, dict):
        raise EndpointAttestationError("launch_plan.environment_invalid")
    environment: dict[str, str] = {}
    for name, setting in environment_raw.items():
        if not isinstance(name, str) or not ENV_RE.fullmatch(name) or not isinstance(setting, str):
            raise EndpointAttestationError("launch_plan.environment_invalid")
        if any(word in name for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "AUTH")):
            raise EndpointAttestationError("launch_plan.environment_contains_secret_name")
        environment[name] = setting
    lane = _exact(raw["lane"], {"name", "baseline", "candidate", "prefix_cache_enabled", "speculative_decode_enabled", "stream", "return_token_ids"}, "launch_plan.lane")
    if lane["name"] != "exact_request_reuse" or lane["baseline"] != "target_only_direct_decode" or lane["prefix_cache_enabled"] is not False or lane["speculative_decode_enabled"] is not False or lane["stream"] is not False or lane["return_token_ids"] is not True:
        raise EndpointAttestationError("launch_plan_lane_not_target_only_exact_cache")
    return {"source_sha256": source_sha, "endpoint": endpoint, "model": {"model_id": model_id, "model_revision": model_revision, "precision_id": precision_id}, "argv": argv, "environment": dict(sorted(environment.items()))}


def _parse_runtime_input(value: Any) -> dict[str, Any]:
    raw = _exact(value, {"schema_version", "kind", "source_sha256", "host", "engine", "vllm_core", "metal_runtime", "resolved_engine_config_sha256", "model"}, "runtime_input")
    if raw["schema_version"] != SCHEMA_VERSION or raw["kind"] != RUNTIME_INPUT_KIND:
        raise EndpointAttestationError("runtime_input_identity_invalid")
    source_sha = _sha(raw["source_sha256"], "runtime_input.source_sha256")
    resolved = _sha(raw["resolved_engine_config_sha256"], "runtime_input.resolved_engine_config_sha256")
    model = raw["model"]
    if not isinstance(model, dict):
        raise EndpointAttestationError("runtime_input.model_invalid")
    model_id = _string(model.get("model_id"), "runtime_input.model.model_id")
    revision = _string(model.get("model_revision"), "runtime_input.model.model_revision", maximum=128)
    trees: dict[str, dict[str, Any]] = {}
    for name in ("engine", "vllm_core", "metal_runtime"):
        fields = {"path", "file_count", "tree_sha256"}
        if name == "engine":
            # The preparation helper pins the exact fork commit in addition to
            # its content-addressed tree.  It is not an unknown extension:
            # retain it in the attestation projection so an operator can
            # inspect both identities without weakening the strict schema.
            fields.add("engine_commit")
        tree = _exact(raw[name], fields, f"runtime_input.{name}")
        path = Path(_string(tree["path"], f"runtime_input.{name}.path"))
        if not path.is_absolute() or path.is_symlink() or not path.is_dir():
            raise EndpointAttestationError(f"runtime_input.{name}.path_unsafe")
        trees[name] = {"path": path, "file_count": _integer(tree["file_count"], f"runtime_input.{name}.file_count", minimum=1), "tree_sha256": _sha(tree["tree_sha256"], f"runtime_input.{name}.tree_sha256")}
        if name == "engine":
            engine_commit = _string(tree["engine_commit"], "runtime_input.engine.engine_commit", maximum=40)
            if not GIT_COMMIT_RE.fullmatch(engine_commit):
                raise EndpointAttestationError("runtime_input.engine.engine_commit_invalid")
            trees[name]["engine_commit"] = engine_commit
    return {"source_sha256": source_sha, "resolved_engine_config_sha256": resolved, "model_id": model_id, "model_revision": revision, "trees": trees}


def _digest_tree(root_value: Path, label: str) -> dict[str, Any]:
    try:
        root = root_value.resolve(strict=True)
    except OSError as exc:
        raise EndpointAttestationError(f"{label}_root_unreadable") from exc
    if root_value.is_symlink() or not root.is_dir():
        raise EndpointAttestationError(f"{label}_root_unsafe")
    rows: list[dict[str, str]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if any(part in SKIP_TREE_PARTS or part.startswith(".venv") for part in relative.parts):
            continue
        if path.is_symlink():
            raise EndpointAttestationError(f"{label}_tree_contains_symlink")
        if path.is_dir():
            continue
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise EndpointAttestationError(f"{label}_tree_contains_nonregular_file")
        before = (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise EndpointAttestationError(f"{label}_tree_unreadable") from exc
        after = path.stat()
        if before != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise EndpointAttestationError(f"{label}_tree_changed_while_hashing")
        rows.append({"path": relative.as_posix(), "sha256": sha256_bytes(payload)})
    if not rows:
        raise EndpointAttestationError(f"{label}_tree_empty")
    return {"path": str(root), "file_count": len(rows), "tree_sha256": sha256_json({"contract": "cx-tree-sha256-v1", "files": rows})}


def _models_url(endpoint_url: str) -> str:
    parsed = urllib.parse.urlsplit(endpoint_url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/v1/models", "", ""))


def _http_models(url: str, timeout_secs: float) -> tuple[dict[str, Any], bytes]:
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(url=url, method="GET", headers={"Accept": "application/json"})
    try:
        with opener.open(request, timeout=timeout_secs) as response:
            if response.status != 200:
                raise EndpointAttestationError(f"endpoint_models_http_status_{response.status}")
            raw = response.read(MAX_HTTP_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise EndpointAttestationError(f"endpoint_models_http_status_{exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise EndpointAttestationError("endpoint_models_unreachable") from exc
    if not raw or len(raw) > MAX_HTTP_BYTES:
        raise EndpointAttestationError("endpoint_models_response_size_invalid")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_duplicate_safe_object, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, EndpointAttestationError) as exc:
        raise EndpointAttestationError("endpoint_models_invalid_json") from exc
    if not isinstance(value, dict):
        raise EndpointAttestationError("endpoint_models_root_invalid")
    return value, raw


def _served_model(models: Mapping[str, Any], expected_id: str) -> dict[str, str]:
    data = models.get("data")
    if not isinstance(data, list) or not data:
        raise EndpointAttestationError("endpoint_models_data_invalid")
    matching: list[dict[str, str]] = []
    for index, row in enumerate(data):
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            raise EndpointAttestationError(f"endpoint_models_data_{index}_invalid")
        if row["id"] == expected_id:
            matching.append({"id": row["id"]})
    if len(matching) != 1:
        raise EndpointAttestationError("endpoint_models_does_not_identify_exactly_one_served_model")
    return matching[0]


def _probe_process(pid: int) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "pid=", "-o", "lstart=", "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EndpointAttestationError("endpoint_process_probe_failed") from exc
    line = completed.stdout.strip()
    if completed.returncode != 0 or not line:
        raise EndpointAttestationError("endpoint_process_not_running")
    match = re.match(r"^\s*(\d+)\s+(.{24})\s+(.+)$", line)
    if match is None or int(match.group(1)) != pid:
        raise EndpointAttestationError("endpoint_process_probe_malformed")
    started = match.group(2).strip()
    command = match.group(3)
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise EndpointAttestationError("endpoint_process_command_unparseable") from exc
    if not argv:
        raise EndpointAttestationError("endpoint_process_command_empty")
    return {"pid": pid, "started_at": started, "command": command, "argv": argv}


def _bind_process(expected_argv: Sequence[str], probe: Mapping[str, Any]) -> dict[str, Any]:
    observed = probe.get("argv")
    if not isinstance(observed, list) or not all(isinstance(item, str) for item in observed):
        raise EndpointAttestationError("endpoint_process_probe_argv_invalid")
    expected = list(expected_argv)
    positions = [index for index in range(len(observed) - len(expected) + 1) if observed[index:index + len(expected)] == expected]
    if len(positions) != 1 or positions[0] + len(expected) != len(observed):
        raise EndpointAttestationError("endpoint_process_command_differs_from_prepared_argv")
    executable = Path(expected[0])
    _, executable_bytes = _safe_existing(executable, "launch_executable", maximum=MAX_LOG_BYTES)
    return {
        "pid": _integer(probe.get("pid"), "endpoint_process.pid", minimum=1),
        "started_at": _string(probe.get("started_at"), "endpoint_process.started_at", maximum=128),
        "observed_command": _string(probe.get("command"), "endpoint_process.observed_command"),
        "observed_command_sha256": sha256_json(observed),
        "prepared_argv_sha256": sha256_json(expected),
        "prepared_argv_match_index": positions[0],
        "launch_executable_path": str(executable),
        "launch_executable_sha256": sha256_bytes(executable_bytes),
    }


def _attestation_sha256(value: Mapping[str, Any]) -> str:
    copy_value = copy.deepcopy(dict(value))
    copy_value["attestation_sha256"] = ""
    return sha256_json(copy_value)


def attest(
    *,
    launch_plan_path: Path,
    runtime_input_path: Path,
    startup_log_path: Path,
    startup_log_snapshot_out: Path,
    pid: int,
    attestation_out: Path,
    process_probe: Callable[[int], dict[str, Any]] = _probe_process,
    models_request: Callable[[str, float], tuple[dict[str, Any], bytes]] = _http_models,
) -> dict[str, Any]:
    """Bind one running prepared endpoint and write immutable snapshot + receipt."""

    if startup_log_snapshot_out == attestation_out:
        raise EndpointAttestationError("startup_log_snapshot_and_attestation_must_differ")
    launch_path, launch_raw, launch_bytes = _read_json(launch_plan_path, "launch_plan")
    runtime_path, runtime_raw, runtime_bytes = _read_json(runtime_input_path, "runtime_input")
    launch = _parse_launch_plan(launch_raw)
    runtime = _parse_runtime_input(runtime_raw)
    if launch["source_sha256"] != runtime["source_sha256"]:
        raise EndpointAttestationError("launch_plan_runtime_input_source_mismatch")
    if launch["model"]["model_id"] != runtime["model_id"] or launch["model"]["model_revision"] != runtime["model_revision"]:
        raise EndpointAttestationError("launch_plan_runtime_input_model_mismatch")
    live_log_path, live_log_bytes = _safe_existing(startup_log_path, "startup_log", maximum=MAX_LOG_BYTES)
    snapshot_path = _new_path(startup_log_snapshot_out, "startup_log_snapshot_out")
    output_path = _new_path(attestation_out, "attestation_out")
    if type(pid) is not int or pid < 1:
        raise EndpointAttestationError("endpoint_pid_invalid")
    probe = process_probe(pid)
    process = _bind_process(launch["argv"], probe)
    trees: dict[str, dict[str, Any]] = {}
    for name, expected in runtime["trees"].items():
        observed = _digest_tree(expected["path"], f"runtime_{name}")
        if observed["file_count"] != expected["file_count"] or observed["tree_sha256"] != expected["tree_sha256"]:
            raise EndpointAttestationError(f"runtime_{name}_tree_differs_from_prepared_input")
        if name == "engine":
            # The commit was schema-validated from the immutable preparation
            # input above; retain it alongside the independently rehashed tree.
            observed["engine_commit"] = expected["engine_commit"]
        trees[name] = observed
    models_url = _models_url(launch["endpoint"]["url"])
    models, models_bytes = models_request(models_url, launch["endpoint"]["timeout_secs"])
    served = _served_model(models, launch["model"]["model_id"])
    _write_new(snapshot_path, live_log_bytes, "startup_log_snapshot_out")
    snapshot_sha = sha256_bytes(live_log_bytes)
    value: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "claim_scope": "physical_local_unattested_endpoint_binding_only",
        "launch_plan": {
            "path": str(launch_path),
            "sha256": sha256_bytes(launch_bytes),
            "source_sha256": launch["source_sha256"],
            "prepared_argv_sha256": sha256_json(launch["argv"]),
            "sanitized_environment": launch["environment"],
            "sanitized_environment_sha256": sha256_json(launch["environment"]),
        },
        "endpoint": {
            "url": launch["endpoint"]["url"],
            "models_url": models_url,
            "models_response_sha256": sha256_bytes(models_bytes),
            "served_model_id": served["id"],
            "model_revision": launch["model"]["model_revision"],
        },
        "process": process,
        "runtime": {
            "runtime_input_path": str(runtime_path),
            "runtime_input_sha256": sha256_bytes(runtime_bytes),
            "resolved_engine_config_sha256": runtime["resolved_engine_config_sha256"],
            "engine": trees["engine"],
            "vllm_core": trees["vllm_core"],
            "metal_runtime": trees["metal_runtime"],
        },
        "startup_log_snapshot": {
            "path": str(snapshot_path),
            "sha256": snapshot_sha,
            "byte_count": len(live_log_bytes),
        },
        "attestation_sha256": "",
    }
    value["attestation_sha256"] = _attestation_sha256(value)
    _write_new(output_path, canonical_json_bytes(value) + b"\n", "attestation_out")
    return {"attestation": str(output_path), "startup_log_snapshot": str(snapshot_path), "attestation_sha256": value["attestation_sha256"], "claim": "local endpoint identity binding only; no performance, promotion, or authorization claim"}


def _parse_attestation(value: Any) -> dict[str, Any]:
    raw = _exact(value, {"schema_version", "kind", "claim_scope", "launch_plan", "endpoint", "process", "runtime", "startup_log_snapshot", "attestation_sha256"}, "endpoint_attestation")
    if raw["schema_version"] != SCHEMA_VERSION or raw["kind"] != KIND or raw["claim_scope"] != "physical_local_unattested_endpoint_binding_only":
        raise EndpointAttestationError("endpoint_attestation_identity_invalid")
    if _sha(raw["attestation_sha256"], "endpoint_attestation.attestation_sha256") != _attestation_sha256(raw):
        raise EndpointAttestationError("endpoint_attestation_self_hash_invalid")
    launch = _exact(raw["launch_plan"], {"path", "sha256", "source_sha256", "prepared_argv_sha256", "sanitized_environment", "sanitized_environment_sha256"}, "endpoint_attestation.launch_plan")
    endpoint = _exact(raw["endpoint"], {"url", "models_url", "models_response_sha256", "served_model_id", "model_revision"}, "endpoint_attestation.endpoint")
    runtime = _exact(raw["runtime"], {"runtime_input_path", "runtime_input_sha256", "resolved_engine_config_sha256", "engine", "vllm_core", "metal_runtime"}, "endpoint_attestation.runtime")
    snapshot = _exact(raw["startup_log_snapshot"], {"path", "sha256", "byte_count"}, "endpoint_attestation.startup_log_snapshot")
    snapshot_path = Path(_string(snapshot["path"], "endpoint_attestation.snapshot.path"))
    if not snapshot_path.is_absolute() or snapshot_path.is_symlink() or not snapshot_path.is_file():
        raise EndpointAttestationError("endpoint_attestation_snapshot_path_unsafe")
    _, snapshot_bytes = _safe_existing(snapshot_path, "endpoint_attestation_snapshot", maximum=MAX_LOG_BYTES)
    if _sha(snapshot["sha256"], "endpoint_attestation.snapshot.sha256") != sha256_bytes(snapshot_bytes) or _integer(snapshot["byte_count"], "endpoint_attestation.snapshot.byte_count") != len(snapshot_bytes):
        raise EndpointAttestationError("endpoint_attestation_snapshot_changed")
    return _stable(raw)


def validate_for_workload(
    path_value: Path | str,
    *,
    endpoint_url: str,
    model_id: str,
    model_revision: str,
    runtime: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a frozen attestation before a cache runner may call HTTP."""

    _path, raw, _bytes = _read_json(path_value, "endpoint_attestation")
    value = _parse_attestation(raw)
    endpoint = value["endpoint"]
    if endpoint["url"] != endpoint_url or endpoint["served_model_id"] != model_id or endpoint["model_revision"] != model_revision:
        raise EndpointAttestationError("endpoint_attestation_workload_endpoint_or_model_mismatch")
    attested_runtime = value["runtime"]
    if attested_runtime["resolved_engine_config_sha256"] != runtime.get("resolved_engine_config_sha256"):
        raise EndpointAttestationError("endpoint_attestation_runtime_config_mismatch")
    return value


def status() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "kind": KIND, "measurement_status": "no_endpoint_attested", "required": ["prepared launch plan", "prepared runtime input", "running matching process", "GET /v1/models served-model proof", "frozen startup-log snapshot", "rehash engine/core/Metal trees"], "claim": "no server started and no performance claim"}


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    attest_parser = sub.add_parser("attest")
    attest_parser.add_argument("--launch-plan", type=Path, required=True)
    attest_parser.add_argument("--runtime-input", type=Path, required=True)
    attest_parser.add_argument("--startup-log", type=Path, required=True)
    attest_parser.add_argument("--startup-log-snapshot-out", type=Path, required=True)
    attest_parser.add_argument("--pid", type=int, required=True)
    attest_parser.add_argument("--attestation-out", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        value = status() if args.command == "status" else attest(launch_plan_path=args.launch_plan, runtime_input_path=args.runtime_input, startup_log_path=args.startup_log, startup_log_snapshot_out=args.startup_log_snapshot_out, pid=args.pid, attestation_out=args.attestation_out)
    except EndpointAttestationError as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(value, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
