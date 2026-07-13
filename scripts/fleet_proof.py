#!/usr/bin/env python3
"""Inventory-driven, machine-readable proof for a heterogeneous CX fleet.

This driver deliberately does not provision hardware.  It can:

* plan a run without executing anything (``--mode dry-run``),
* exercise the full assertion/ledger path with embedded mock evidence, or
* connect to explicitly inventoried local/SSH nodes (``--mode live``).

Live readiness and collector commands have small JSON contracts documented in
``docs/fleet-proof/README.md``.  The driver maps completed task receipts back to
the workers observed during readiness, then proves physical-machine, worker,
substrate, and lane cardinality.  Ephemeral RunPod entries are fail-closed: a
live run is rejected unless each one has an executable teardown policy, and the
teardown is attempted on every exit path.  There is no pod-create API call in
this file.

Only Python's standard library is used so this can run from a clean checkout.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SCHEMA_VERSION = 1
RUNPOD_API = "https://api.runpod.io/graphql"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACTS = REPO_ROOT / ".artifacts" / "fleet-proof"
SECRET_ENV_NAMES = (
    "RUNPOD_API_KEY",
    "CX_WORKER_TOKEN",
    "CX_ADMIN_KEY",
    "ADMIN_KEY",
    "STRIPE_SECRET_KEY",
    "DATABASE_URL",
)
PLACEHOLDER_RE = re.compile(r"(?:REPLACE(?:_WITH)?|<[^>]+>)", re.IGNORECASE)
HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ConfigError(ValueError):
    """The inventory cannot safely or unambiguously describe a proof run."""


class ProofError(RuntimeError):
    """A live/mock observation failed the declared proof contract."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_name(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._-")
    return clean or "item"


def _secret_values() -> List[str]:
    return [os.environ[name] for name in SECRET_ENV_NAMES if len(os.environ.get(name, "")) >= 4]


def redact_text(value: Any) -> str:
    text = str(value)
    for secret in _secret_values():
        text = text.replace(secret, "[REDACTED_ENV_SECRET]")
    text = re.sub(r"(?i)(authorization\s*:\s*bearer\s+)[^\s\"']+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(api_key=)[^&\s\"']+", r"\1[REDACTED]", text)
    text = re.sub(r"\brpa_[A-Za-z0-9_.-]+", "rpa_[REDACTED]", text)
    return text


def redact_object(value: Any) -> Any:
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            # ``api_key_env`` names where a secret is read from; the name is
            # reproducibility metadata, not a credential. Preserve it while
            # still redacting fields that could contain literal secret values.
            if re.search(r"(?i)(secret|token|password|api.?key)", key_text) and not key_text.lower().endswith("_env"):
                out[str(key)] = "[REDACTED]"
            else:
                out[str(key)] = redact_object(child)
        return out
    if isinstance(value, list):
        return [redact_object(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def ensure_keys(value: Mapping[str, Any], allowed: Iterable[str], where: str) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ConfigError(f"{where}: unknown key(s): {', '.join(unknown)}")


def require_string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: expected a non-empty string")
    return value.strip()


def require_slug(value: Any, where: str) -> str:
    text = require_string(value, where)
    if not SLUG_RE.fullmatch(text):
        raise ConfigError(f"{where}: use only letters, digits, '.', '_' or '-' (got {text!r})")
    return text


def require_int(value: Any, where: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{where}: expected an integer >= {minimum}")
    return value


def require_number(value: Any, where: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) < minimum:
        raise ConfigError(f"{where}: expected a number >= {minimum}")
    return float(value)


def require_bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: expected true or false")
    return value


def find_placeholders(value: Any, path: str = "inventory") -> List[str]:
    found: List[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            found.extend(find_placeholders(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(find_placeholders(child, f"{path}[{index}]"))
    elif isinstance(value, str) and PLACEHOLDER_RE.search(value):
        found.append(path)
    return found


def load_inventory(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"inventory not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"inventory is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError("inventory root must be an object")
    return value


def _validate_count_map(value: Any, where: str) -> Dict[str, int]:
    if not isinstance(value, dict) or not value:
        raise ConfigError(f"{where}: expected a non-empty object of name -> minimum count")
    out: Dict[str, int] = {}
    for key, count in value.items():
        name = require_slug(key, f"{where} key")
        out[name] = require_int(count, f"{where}.{name}", 1)
    return out


def validate_inventory(inventory: Dict[str, Any], mode: str = "dry-run") -> None:
    """Validate schema, static cardinality, and mode-specific safety preconditions."""
    ensure_keys(
        inventory,
        {"schema_version", "name", "requirements", "nodes", "workflow", "readiness", "collection"},
        "inventory",
    )
    if inventory.get("schema_version") != SCHEMA_VERSION:
        raise ConfigError(f"inventory.schema_version must be {SCHEMA_VERSION}")
    require_string(inventory.get("name"), "inventory.name")

    requirements = inventory.get("requirements")
    if not isinstance(requirements, dict):
        raise ConfigError("inventory.requirements must be an object")
    required_requirement_keys = {
        "min_total_nodes",
        "min_distinct_workers",
        "min_distinct_machines",
        "substrates",
        "lanes",
        "min_receipts_per_node",
        "require_artifact_sha256",
        "require_verification_class",
        "require_settlement",
    }
    ensure_keys(requirements, required_requirement_keys, "inventory.requirements")
    missing = sorted(required_requirement_keys - set(requirements))
    if missing:
        raise ConfigError(f"inventory.requirements missing: {', '.join(missing)}")
    min_total = require_int(requirements["min_total_nodes"], "requirements.min_total_nodes", 1)
    min_workers = require_int(requirements["min_distinct_workers"], "requirements.min_distinct_workers", 1)
    min_machines = require_int(requirements["min_distinct_machines"], "requirements.min_distinct_machines", 1)
    min_receipts = require_int(requirements["min_receipts_per_node"], "requirements.min_receipts_per_node", 1)
    require_bool(requirements["require_artifact_sha256"], "requirements.require_artifact_sha256")
    require_bool(requirements["require_verification_class"], "requirements.require_verification_class")
    require_bool(requirements["require_settlement"], "requirements.require_settlement")
    substrate_min = _validate_count_map(requirements["substrates"], "requirements.substrates")
    lane_min = _validate_count_map(requirements["lanes"], "requirements.lanes")

    nodes = inventory.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ConfigError("inventory.nodes must be a non-empty array")
    if min_total > len(nodes) or min_workers > len(nodes) or min_machines > len(nodes):
        raise ConfigError("declared node/worker/machine minima exceed the inventory size")
    if min_receipts < 1:
        raise ConfigError("requirements.min_receipts_per_node must be at least 1")

    node_ids: set[str] = set()
    resources: set[str] = set()
    declared_substrates: Dict[str, int] = {}
    declared_lanes: Dict[str, int] = {}
    for index, node in enumerate(nodes):
        where = f"inventory.nodes[{index}]"
        if not isinstance(node, dict):
            raise ConfigError(f"{where} must be an object")
        ensure_keys(node, {"id", "substrate", "lane", "provider", "transport", "expect", "commands", "mock"}, where)
        node_id = require_slug(node.get("id"), f"{where}.id")
        if node_id in node_ids:
            raise ConfigError(f"duplicate node id: {node_id}")
        node_ids.add(node_id)
        substrate = require_slug(node.get("substrate"), f"{where}.substrate")
        lane = require_slug(node.get("lane"), f"{where}.lane")
        declared_substrates[substrate] = declared_substrates.get(substrate, 0) + 1
        declared_lanes[lane] = declared_lanes.get(lane, 0) + 1

        provider = node.get("provider")
        if not isinstance(provider, dict):
            raise ConfigError(f"{where}.provider must be an object")
        ensure_keys(provider, {"kind", "resource_id", "ephemeral", "cleanup"}, f"{where}.provider")
        provider_kind = require_slug(provider.get("kind"), f"{where}.provider.kind")
        resource_id = require_string(provider.get("resource_id"), f"{where}.provider.resource_id")
        if resource_id in resources:
            raise ConfigError(f"provider resource_id must be unique; repeated: {resource_id}")
        resources.add(resource_id)
        ephemeral = require_bool(provider.get("ephemeral"), f"{where}.provider.ephemeral")
        cleanup = provider.get("cleanup")
        if cleanup is not None:
            if not isinstance(cleanup, dict):
                raise ConfigError(f"{where}.provider.cleanup must be an object")
            ensure_keys(cleanup, {"kind", "api_key_env", "api_url", "command"}, f"{where}.provider.cleanup")
            cleanup_kind = require_slug(cleanup.get("kind"), f"{where}.provider.cleanup.kind")
            if cleanup_kind not in {"runpod_api", "command"}:
                raise ConfigError(f"{where}.provider.cleanup.kind must be runpod_api or command")
            if cleanup_kind == "runpod_api":
                if provider_kind != "runpod":
                    raise ConfigError(f"{where}: runpod_api cleanup requires provider.kind=runpod")
                if "api_key_env" in cleanup:
                    require_slug(cleanup["api_key_env"], f"{where}.provider.cleanup.api_key_env")
                if "api_url" in cleanup:
                    require_string(cleanup["api_url"], f"{where}.provider.cleanup.api_url")
            if cleanup_kind == "command":
                require_string(cleanup.get("command"), f"{where}.provider.cleanup.command")
        if ephemeral and cleanup is None:
            raise ConfigError(f"{where}: ephemeral provider requires a provider.cleanup policy")

        transport = node.get("transport")
        if not isinstance(transport, dict):
            raise ConfigError(f"{where}.transport must be an object")
        transport_kind = require_slug(transport.get("kind"), f"{where}.transport.kind")
        if transport_kind == "mock":
            ensure_keys(transport, {"kind"}, f"{where}.transport")
        elif transport_kind == "local":
            ensure_keys(transport, {"kind"}, f"{where}.transport")
        elif transport_kind == "ssh":
            ensure_keys(
                transport,
                {"kind", "target", "port", "identity_file", "host_key_policy", "known_hosts_file", "connect_timeout_seconds"},
                f"{where}.transport",
            )
            require_string(transport.get("target"), f"{where}.transport.target")
            if "port" in transport:
                require_int(transport["port"], f"{where}.transport.port", 1)
            if "identity_file" in transport:
                require_string(transport["identity_file"], f"{where}.transport.identity_file")
            policy = transport.get("host_key_policy", "strict")
            if policy not in {"strict", "accept-new"}:
                raise ConfigError(f"{where}.transport.host_key_policy must be strict or accept-new")
            if "known_hosts_file" in transport:
                require_string(transport["known_hosts_file"], f"{where}.transport.known_hosts_file")
            if "connect_timeout_seconds" in transport:
                require_int(transport["connect_timeout_seconds"], f"{where}.transport.connect_timeout_seconds", 1)
        else:
            raise ConfigError(f"{where}.transport.kind must be mock, local, or ssh")

        expect = node.get("expect")
        if not isinstance(expect, dict):
            raise ConfigError(f"{where}.expect must be an object")
        ensure_keys(expect, {"engine", "hardware_class", "build_hash"}, f"{where}.expect")
        require_string(expect.get("engine"), f"{where}.expect.engine")
        require_string(expect.get("hardware_class"), f"{where}.expect.hardware_class")
        if "build_hash" in expect:
            require_string(expect["build_hash"], f"{where}.expect.build_hash")

        commands = node.get("commands", {})
        if not isinstance(commands, dict):
            raise ConfigError(f"{where}.commands must be an object")
        ensure_keys(commands, {"start", "ready", "cleanup"}, f"{where}.commands")
        for command_name, command in commands.items():
            require_string(command, f"{where}.commands.{command_name}")

        mock = node.get("mock")
        if mock is not None:
            if not isinstance(mock, dict):
                raise ConfigError(f"{where}.mock must be an object")
            ensure_keys(mock, {"ready", "start_rc", "cleanup_rc"}, f"{where}.mock")
            if "start_rc" in mock:
                require_int(mock["start_rc"], f"{where}.mock.start_rc", 0)
            if "cleanup_rc" in mock:
                require_int(mock["cleanup_rc"], f"{where}.mock.cleanup_rc", 0)

    for name, count in substrate_min.items():
        if declared_substrates.get(name, 0) < count:
            raise ConfigError(
                f"inventory has {declared_substrates.get(name, 0)} {name} node(s), below required {count}"
            )
    for name, count in lane_min.items():
        if declared_lanes.get(name, 0) < count:
            raise ConfigError(f"inventory has {declared_lanes.get(name, 0)} {name} lane node(s), below required {count}")

    for section_name in ("readiness", "collection"):
        section = inventory.get(section_name)
        if not isinstance(section, dict):
            raise ConfigError(f"inventory.{section_name} must be an object")
        ensure_keys(section, {"attempts", "poll_interval_seconds", "command_timeout_seconds"}, f"inventory.{section_name}")
        require_int(section.get("attempts"), f"{section_name}.attempts", 1)
        require_number(section.get("poll_interval_seconds"), f"{section_name}.poll_interval_seconds", 0)
        require_int(section.get("command_timeout_seconds"), f"{section_name}.command_timeout_seconds", 1)

    workflow = inventory.get("workflow")
    if not isinstance(workflow, dict):
        raise ConfigError("inventory.workflow must be an object")
    ensure_keys(workflow, {"commands", "mock"}, "inventory.workflow")
    workflow_commands = workflow.get("commands", {})
    if not isinstance(workflow_commands, dict):
        raise ConfigError("inventory.workflow.commands must be an object")
    ensure_keys(workflow_commands, {"submit", "collect", "cleanup"}, "inventory.workflow.commands")
    for name, command in workflow_commands.items():
        require_string(command, f"inventory.workflow.commands.{name}")
    workflow_mock = workflow.get("mock")
    if workflow_mock is not None:
        if not isinstance(workflow_mock, dict):
            raise ConfigError("inventory.workflow.mock must be an object")
        ensure_keys(workflow_mock, {"submit", "collect", "cleanup_rc"}, "inventory.workflow.mock")
        if "cleanup_rc" in workflow_mock:
            require_int(workflow_mock["cleanup_rc"], "inventory.workflow.mock.cleanup_rc", 0)

    if mode not in {"dry-run", "mock", "live"}:
        raise ConfigError(f"unsupported mode: {mode}")
    if mode == "mock":
        for node in nodes:
            if node["transport"]["kind"] != "mock" or not isinstance(node.get("mock", {}).get("ready"), dict):
                raise ConfigError("mock mode requires transport.kind=mock and mock.ready for every node")
        if not isinstance(workflow_mock, dict) or not isinstance(workflow_mock.get("submit"), dict) or not isinstance(workflow_mock.get("collect"), dict):
            raise ConfigError("mock mode requires workflow.mock.submit and workflow.mock.collect objects")
    if mode == "live":
        placeholders = find_placeholders(inventory)
        if placeholders:
            raise ConfigError("live mode refuses unresolved placeholders at: " + ", ".join(placeholders[:12]))
        for index, node in enumerate(nodes):
            where = f"inventory.nodes[{index}]"
            if node["transport"]["kind"] == "mock":
                raise ConfigError("live mode refuses mock transports")
            commands = node.get("commands", {})
            if "ready" not in commands:
                raise ConfigError(f"{where}: live mode requires commands.ready")
            if "start" in commands and "cleanup" not in commands:
                raise ConfigError(f"{where}: a live start command requires a cleanup command")
            provider = node["provider"]
            cleanup = provider.get("cleanup")
            if provider["ephemeral"] and cleanup and cleanup["kind"] == "runpod_api":
                env_name = cleanup.get("api_key_env", "RUNPOD_API_KEY")
                if not os.environ.get(env_name):
                    raise ConfigError(
                        f"{where}: {env_name} is unset; refusing to touch ephemeral {provider['resource_id']} without teardown credentials"
                    )
        if "submit" not in workflow_commands or "collect" not in workflow_commands:
            raise ConfigError("live mode requires workflow.commands.submit and workflow.commands.collect")


@dataclasses.dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    argv_kind: str
    timed_out: bool = False


class ArtifactWriter:
    def __init__(self, root: Path, run_id: str):
        self.root = root
        try:
            self.root.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise ConfigError(
                f"artifact directory already exists: {self.root}; choose a new --run-id"
            ) from exc
        self.run_id = run_id
        self.ledger = self.root / "events.jsonl"
        self._lock = threading.Lock()

    def event(self, event: str, status: str, **fields: Any) -> None:
        row = {
            "schema_version": SCHEMA_VERSION,
            "timestamp": utc_now(),
            "run_id": self.run_id,
            "event": event,
            "status": status,
            **redact_object(fields),
        }
        line = json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
        with self._lock:
            with self.ledger.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()

    def write_text(self, relative: str, value: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return path

    def write_json(self, relative: str, value: Any) -> Path:
        return self.write_text(relative, json.dumps(redact_object(value), indent=2, sort_keys=True) + "\n")

    def command_result(self, scope: str, stage: str, attempt: int, result: CommandResult) -> Dict[str, Any]:
        prefix = f"commands/{safe_name(scope)}/{safe_name(stage)}-{attempt}"
        stdout = redact_text(result.stdout)
        stderr = redact_text(result.stderr)
        out_path = self.write_text(prefix + ".stdout.txt", stdout)
        err_path = self.write_text(prefix + ".stderr.txt", stderr)
        meta = {
            "returncode": result.returncode,
            "duration_seconds": round(result.duration_seconds, 6),
            "timed_out": result.timed_out,
            "transport": result.argv_kind,
            "stdout_sha256": sha256_file(out_path),
            "stderr_sha256": sha256_file(err_path),
            "stdout_artifact": str(out_path.relative_to(self.root)),
            "stderr_artifact": str(err_path.relative_to(self.root)),
        }
        self.write_json(prefix + ".json", meta)
        return meta

    def finalize_manifest(self) -> Path:
        rows = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.name == "artifact_manifest.json":
                continue
            rows.append(
                {
                    "path": str(path.relative_to(self.root)),
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
        return self.write_json("artifact_manifest.json", {"schema_version": SCHEMA_VERSION, "files": rows})


def command_environment(run_id: str, artifact_root: Path, node: Optional[Mapping[str, Any]] = None, **extra: str) -> Dict[str, str]:
    env = {
        "CX_FLEET_PROOF_RUN_ID": run_id,
        "CX_FLEET_PROOF_ARTIFACT_DIR": str(artifact_root),
    }
    if node is not None:
        env.update(
            {
                "CX_FLEET_PROOF_NODE_ID": str(node["id"]),
                "CX_FLEET_PROVIDER_RESOURCE_ID": str(node["provider"]["resource_id"]),
                "CX_FLEET_EXPECTED_SUBSTRATE": str(node["substrate"]),
                "CX_FLEET_EXPECTED_LANE": str(node["lane"]),
            }
        )
    env.update({key: str(value) for key, value in extra.items() if value is not None})
    return env


def execute_command(
    transport: Mapping[str, Any],
    command: str,
    env: Mapping[str, str],
    timeout_seconds: int,
) -> CommandResult:
    """Execute an operator-authored command without using ``shell=True`` locally."""
    kind = transport["kind"]
    process_env = os.environ.copy()
    process_env.update(env)
    if kind == "local":
        argv = ["/bin/sh", "-lc", command]
    elif kind == "ssh":
        connect_timeout = int(transport.get("connect_timeout_seconds", 12))
        argv = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={connect_timeout}",
            "-o", "ServerAliveInterval=20",
            "-o", "ServerAliveCountMax=3",
        ]
        policy = transport.get("host_key_policy", "strict")
        argv += ["-o", f"StrictHostKeyChecking={'accept-new' if policy == 'accept-new' else 'yes'}"]
        if transport.get("known_hosts_file"):
            argv += ["-o", f"UserKnownHostsFile={os.path.expanduser(transport['known_hosts_file'])}"]
        if transport.get("identity_file"):
            argv += ["-i", os.path.expanduser(transport["identity_file"])]
        if transport.get("port"):
            argv += ["-p", str(transport["port"])]
        remote_env = " ".join(f"{key}={shlex.quote(value)}" for key, value in sorted(env.items()))
        remote = f"env {remote_env} /bin/sh -lc {shlex.quote(command)}"
        argv += [transport["target"], remote]
    else:
        raise ProofError(f"cannot execute transport kind {kind!r}")

    started = time.monotonic()
    try:
        result = subprocess.run(
            argv,
            cwd=str(REPO_ROOT),
            env=process_env,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        return CommandResult(
            result.returncode,
            result.stdout,
            result.stderr,
            time.monotonic() - started,
            kind,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return CommandResult(124, stdout, stderr + f"\ncommand timed out after {timeout_seconds}s", time.monotonic() - started, kind, True)
    except OSError as exc:
        return CommandResult(127, "", str(exc), time.monotonic() - started, kind)


def parse_final_json(stdout: str, where: str) -> Dict[str, Any]:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise ProofError(f"{where}: command emitted no JSON")
    try:
        value = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise ProofError(f"{where}: final non-empty stdout line is not JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ProofError(f"{where}: final JSON value must be an object")
    return value


def _mock_command_result(returncode: int = 0) -> CommandResult:
    return CommandResult(returncode, "", "", 0.0, "mock")


def expand_mock(value: Any, env: Mapping[str, str]) -> Any:
    """Expand only explicit ``${CX_FLEET_*}`` markers in mock fixtures."""
    if isinstance(value, dict):
        return {key: expand_mock(child, env) for key, child in value.items()}
    if isinstance(value, list):
        return [expand_mock(child, env) for child in value]
    if isinstance(value, str):
        out = value
        for key, replacement in env.items():
            if key.startswith("CX_FLEET_"):
                out = out.replace("${" + key + "}", replacement)
        return out
    return value


def run_node_stage(
    node: Mapping[str, Any],
    stage: str,
    mode: str,
    timeout_seconds: int,
    run_id: str,
    writer: ArtifactWriter,
) -> CommandResult:
    if mode == "mock":
        returncode = int(node.get("mock", {}).get(f"{stage}_rc", 0))
        result = _mock_command_result(returncode)
    else:
        command = node.get("commands", {}).get(stage)
        if not command:
            result = _mock_command_result(0)
            result.argv_kind = "not-configured"
        else:
            result = execute_command(
                node["transport"],
                command,
                command_environment(run_id, writer.root, node),
                timeout_seconds,
            )
    meta = writer.command_result(str(node["id"]), stage, 1, result)
    writer.event("node_command", "pass" if result.returncode == 0 else "fail", node_id=node["id"], stage=stage, **meta)
    return result


def probe_node(
    node: Mapping[str, Any],
    mode: str,
    readiness: Mapping[str, Any],
    run_id: str,
    writer: ArtifactWriter,
) -> Dict[str, Any]:
    attempts = int(readiness["attempts"])
    interval = float(readiness["poll_interval_seconds"])
    timeout = int(readiness["command_timeout_seconds"])
    last_error = "not attempted"
    for attempt in range(1, attempts + 1):
        if mode == "mock":
            payload = dict(node["mock"]["ready"])
            result = CommandResult(0, json.dumps(payload) + "\n", "", 0.0, "mock")
        else:
            result = execute_command(
                node["transport"],
                node["commands"]["ready"],
                command_environment(run_id, writer.root, node),
                timeout,
            )
        meta = writer.command_result(str(node["id"]), "ready", attempt, result)
        if result.returncode == 0:
            try:
                payload = parse_final_json(result.stdout, f"node {node['id']} readiness")
                if payload.get("ok") is True:
                    writer.event("node_ready", "pass", node_id=node["id"], attempt=attempt, **meta)
                    return payload
                last_error = f"readiness ok was {payload.get('ok')!r}"
            except ProofError as exc:
                last_error = str(exc)
        else:
            last_error = f"readiness command rc={result.returncode}"
        writer.event("node_ready", "retry", node_id=node["id"], attempt=attempt, reason=last_error, **meta)
        if attempt < attempts and interval:
            time.sleep(interval)
    raise ProofError(f"node {node['id']} did not become ready after {attempts} attempt(s): {last_error}")


def _valid_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


class Assertions:
    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []
        self.errors: List[str] = []

    def check(self, name: str, passed: bool, detail: str) -> None:
        self.rows.append({"name": name, "passed": bool(passed), "detail": detail})
        if not passed:
            self.errors.append(f"{name}: {detail}")

    def raise_if_failed(self, prefix: str) -> None:
        if self.errors:
            raise ProofError(prefix + ": " + "; ".join(self.errors))


def assert_readiness(
    inventory: Mapping[str, Any],
    observations: Mapping[str, Dict[str, Any]],
    assertions: Assertions,
) -> None:
    nodes = inventory["nodes"]
    for node in nodes:
        node_id = node["id"]
        ready = observations.get(node_id, {})
        required = (
            "schema_version",
            "ok",
            "node_id",
            "machine_id",
            "provider_resource_id",
            "worker_id",
            "substrate",
            "lane",
            "engine",
            "build_hash",
            "hardware_class",
        )
        missing = [key for key in required if key not in ready]
        assertions.check(f"ready.{node_id}.fields", not missing, "all required fields present" if not missing else f"missing {missing}")
        if missing:
            continue
        schema_ok = (
            isinstance(ready["schema_version"], int)
            and not isinstance(ready["schema_version"], bool)
            and ready["schema_version"] == SCHEMA_VERSION
        )
        assertions.check(f"ready.{node_id}.schema", schema_ok, f"schema_version={ready['schema_version']!r}")
        assertions.check(f"ready.{node_id}.ok", ready["ok"] is True, f"ok={ready['ok']!r}")
        assertions.check(f"ready.{node_id}.node_id", ready["node_id"] == node_id, f"observed {ready['node_id']!r}")
        assertions.check(
            f"ready.{node_id}.resource",
            ready["provider_resource_id"] == node["provider"]["resource_id"],
            f"observed {ready['provider_resource_id']!r}",
        )
        assertions.check(f"ready.{node_id}.worker_uuid", _valid_uuid(ready["worker_id"]), f"worker_id={ready['worker_id']!r}")
        assertions.check(f"ready.{node_id}.machine_id", bool(str(ready["machine_id"]).strip()), f"machine_id={ready['machine_id']!r}")
        assertions.check(f"ready.{node_id}.substrate", ready["substrate"] == node["substrate"], f"observed {ready['substrate']!r}")
        assertions.check(f"ready.{node_id}.lane", ready["lane"] == node["lane"], f"observed {ready['lane']!r}")
        assertions.check(f"ready.{node_id}.engine", ready["engine"] == node["expect"]["engine"], f"observed {ready['engine']!r}")
        assertions.check(
            f"ready.{node_id}.hardware_class",
            ready["hardware_class"] == node["expect"]["hardware_class"],
            f"observed {ready['hardware_class']!r}",
        )
        expected_hash = node["expect"].get("build_hash")
        assertions.check(
            f"ready.{node_id}.build_hash",
            bool(str(ready["build_hash"]).strip()) and (expected_hash is None or ready["build_hash"] == expected_hash),
            f"observed {ready['build_hash']!r}",
        )

    valid = [observations[node["id"]] for node in nodes if node["id"] in observations]
    workers = [str(row.get("worker_id", "")) for row in valid]
    machines = [str(row.get("machine_id", "")) for row in valid]
    req = inventory["requirements"]
    assertions.check("ready.worker_ids_unique", len(set(workers)) == len(workers), f"{len(set(workers))}/{len(workers)} unique")
    assertions.check("ready.min_distinct_workers", len(set(workers)) >= req["min_distinct_workers"], f"observed {len(set(workers))}, required {req['min_distinct_workers']}")
    assertions.check("ready.min_distinct_machines", len(set(machines)) >= req["min_distinct_machines"], f"observed {len(set(machines))}, required {req['min_distinct_machines']}")
    assertions.raise_if_failed("readiness assertions failed")


def run_workflow_command(
    inventory: Mapping[str, Any],
    stage: str,
    mode: str,
    env: Mapping[str, str],
    writer: ArtifactWriter,
    attempts: int = 1,
    interval: float = 0.0,
    timeout_seconds: int = 300,
) -> Dict[str, Any]:
    last_error = "not attempted"
    for attempt in range(1, attempts + 1):
        if mode == "mock":
            payload = expand_mock(inventory["workflow"]["mock"][stage], env)
            result = CommandResult(0, json.dumps(payload) + "\n", "", 0.0, "mock")
        else:
            command = inventory["workflow"]["commands"][stage]
            result = execute_command({"kind": "local"}, command, env, timeout_seconds)
        meta = writer.command_result("workflow", stage, attempt, result)
        if result.returncode == 0:
            try:
                payload = parse_final_json(result.stdout, f"workflow {stage}")
                if payload.get("ok") is True:
                    writer.event(f"workflow_{stage}", "pass", attempt=attempt, **meta)
                    return payload
                last_error = f"workflow {stage} ok was {payload.get('ok')!r}"
            except ProofError as exc:
                last_error = str(exc)
        else:
            last_error = f"workflow {stage} rc={result.returncode}"
        writer.event(f"workflow_{stage}", "retry" if attempt < attempts else "fail", attempt=attempt, reason=last_error, **meta)
        if attempt < attempts and interval:
            time.sleep(interval)
    raise ProofError(f"workflow {stage} failed after {attempts} attempt(s): {last_error}")


def validate_submission(submit: Mapping[str, Any], run_id: str, assertions: Assertions) -> Tuple[str, List[str]]:
    required = {"schema_version", "ok", "proof_run_id", "workload_id", "job_ids"}
    missing = sorted(required - set(submit))
    assertions.check("submit.fields", not missing, "all required fields present" if not missing else f"missing {missing}")
    if missing:
        assertions.raise_if_failed("submission assertions failed")
    workload_value = submit["workload_id"]
    workload_id = workload_value if isinstance(workload_value, str) else ""
    job_ids = submit["job_ids"] if isinstance(submit["job_ids"], list) else []
    schema_ok = isinstance(submit["schema_version"], int) and not isinstance(submit["schema_version"], bool) and submit["schema_version"] == SCHEMA_VERSION
    assertions.check("submit.schema", schema_ok, f"schema_version={submit['schema_version']!r}")
    assertions.check("submit.ok", submit["ok"] is True, f"ok={submit['ok']!r}")
    assertions.check("submit.run_id", submit["proof_run_id"] == run_id, f"observed {submit['proof_run_id']!r}")
    assertions.check("submit.workload_id", isinstance(workload_value, str) and bool(workload_id.strip()), f"workload_id={workload_value!r}")
    assertions.check("submit.job_ids", bool(job_ids) and all(isinstance(v, str) and v for v in job_ids), f"job count={len(job_ids)}")
    all_string_ids = all(isinstance(v, str) and v for v in job_ids)
    unique_count = len(set(job_ids)) if all_string_ids else 0
    assertions.check("submit.job_ids_unique", all_string_ids and len(job_ids) == unique_count, f"{unique_count}/{len(job_ids)} unique")
    assertions.raise_if_failed("submission assertions failed")
    return workload_id, list(job_ids)


def assert_receipts(
    inventory: Mapping[str, Any],
    readiness: Mapping[str, Dict[str, Any]],
    submit: Mapping[str, Any],
    collected: Mapping[str, Any],
    run_id: str,
    assertions: Assertions,
) -> List[Dict[str, Any]]:
    workload_id, job_ids = validate_submission(submit, run_id, assertions)
    required = {"schema_version", "ok", "proof_run_id", "workload_id", "receipts"}
    missing = sorted(required - set(collected))
    assertions.check("collect.fields", not missing, "all required fields present" if not missing else f"missing {missing}")
    if missing:
        assertions.raise_if_failed("collector assertions failed")
    receipts = collected["receipts"] if isinstance(collected["receipts"], list) else []
    collect_schema_ok = (
        isinstance(collected["schema_version"], int)
        and not isinstance(collected["schema_version"], bool)
        and collected["schema_version"] == SCHEMA_VERSION
    )
    assertions.check("collect.schema", collect_schema_ok, f"schema_version={collected['schema_version']!r}")
    assertions.check("collect.ok", collected["ok"] is True, f"ok={collected['ok']!r}")
    assertions.check("collect.run_id", collected["proof_run_id"] == run_id, f"observed {collected['proof_run_id']!r}")
    assertions.check("collect.workload_id", collected["workload_id"] == workload_id, f"observed {collected['workload_id']!r}")
    assertions.check("collect.receipts_nonempty", bool(receipts), f"receipt count={len(receipts)}")

    by_worker = {str(ready["worker_id"]): (node_id, ready) for node_id, ready in readiness.items()}
    seen_tasks: set[str] = set()
    receipt_workers: set[str] = set()
    counts_by_node: Dict[str, int] = {node["id"]: 0 for node in inventory["nodes"]}
    annotated: List[Dict[str, Any]] = []
    for index, receipt in enumerate(receipts):
        label = f"receipt[{index}]"
        if not isinstance(receipt, dict):
            assertions.check(f"{label}.object", False, "receipt is not an object")
            continue
        receipt_required = {"job_id", "task_id", "worker_id", "status"}
        receipt_missing = sorted(receipt_required - set(receipt))
        assertions.check(f"{label}.fields", not receipt_missing, "required fields present" if not receipt_missing else f"missing {receipt_missing}")
        if receipt_missing:
            continue
        job_id = str(receipt["job_id"])
        task_id = str(receipt["task_id"])
        worker_id = str(receipt["worker_id"])
        assertions.check(f"{label}.job", job_id in job_ids, f"job_id={job_id!r}")
        assertions.check(f"{label}.status", receipt["status"] == "complete", f"status={receipt['status']!r}")
        assertions.check(f"{label}.task_unique", bool(task_id) and task_id not in seen_tasks, f"task_id={task_id!r}")
        seen_tasks.add(task_id)
        known_worker = worker_id in by_worker
        assertions.check(f"{label}.known_worker", known_worker, f"worker_id={worker_id!r}")
        if not known_worker:
            continue
        node_id, ready = by_worker[worker_id]
        receipt_workers.add(worker_id)
        counts_by_node[node_id] += 1
        if inventory["requirements"]["require_artifact_sha256"]:
            digest = receipt.get("artifact_sha256")
            assertions.check(f"{label}.artifact_sha256", isinstance(digest, str) and bool(HEX64_RE.fullmatch(digest)), f"artifact_sha256={digest!r}")
        if inventory["requirements"]["require_verification_class"]:
            expected_class = f"{ready['engine']}|{ready['build_hash']}"
            assertions.check(f"{label}.verification_class", receipt.get("verification_class") == expected_class, f"observed {receipt.get('verification_class')!r}, expected {expected_class!r}")
        if inventory["requirements"]["require_settlement"]:
            assertions.check(f"{label}.settled", receipt.get("settled") is True, f"settled={receipt.get('settled')!r}")
        annotated.append({**receipt, "node_id": node_id, "substrate": ready["substrate"], "lane": ready["lane"]})

    req = inventory["requirements"]
    for node_id, count in counts_by_node.items():
        assertions.check(f"receipts.node.{node_id}", count >= req["min_receipts_per_node"], f"observed {count}, required {req['min_receipts_per_node']}")
    receipt_machines = {str(by_worker[worker][1]["machine_id"]) for worker in receipt_workers}
    assertions.check("receipts.min_total_nodes", len(receipt_workers) >= req["min_total_nodes"], f"observed {len(receipt_workers)}, required {req['min_total_nodes']}")
    assertions.check("receipts.min_distinct_workers", len(receipt_workers) >= req["min_distinct_workers"], f"observed {len(receipt_workers)}, required {req['min_distinct_workers']}")
    assertions.check("receipts.min_distinct_machines", len(receipt_machines) >= req["min_distinct_machines"], f"observed {len(receipt_machines)}, required {req['min_distinct_machines']}")

    for substrate, minimum in req["substrates"].items():
        observed = len({worker for worker in receipt_workers if by_worker[worker][1]["substrate"] == substrate})
        assertions.check(f"receipts.substrate.{substrate}", observed >= minimum, f"observed {observed}, required {minimum}")
    for lane, minimum in req["lanes"].items():
        observed = len({worker for worker in receipt_workers if by_worker[worker][1]["lane"] == lane})
        assertions.check(f"receipts.lane.{lane}", observed >= minimum, f"observed {observed}, required {minimum}")
    assertions.raise_if_failed("receipt assertions failed")
    return annotated


def terminate_runpod(provider: Mapping[str, Any], retries: int = 2) -> Tuple[bool, str]:
    """Terminate one explicit pod id.  This function contains no provisioning path."""
    cleanup = provider["cleanup"]
    env_name = cleanup.get("api_key_env", "RUNPOD_API_KEY")
    key = os.environ.get(env_name, "")
    if not key:
        return False, f"{env_name} unset"
    api_url = cleanup.get("api_url", RUNPOD_API)
    pod_id = provider["resource_id"]
    query = "mutation($i:String!){ podTerminate(input:{podId:$i}) }"
    body = json.dumps({"query": query, "variables": {"i": pod_id}}).encode("utf-8")
    url = api_url + ("&" if "?" in api_url else "?") + urllib.parse.urlencode({"api_key": key})
    last_error = "not attempted"
    for attempt in range(retries + 1):
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                "User-Agent": "cx-fleet-proof/1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                payload = json.loads(response.read())
            errors = payload.get("errors") if isinstance(payload, dict) else None
            if errors:
                message = json.dumps(errors, sort_keys=True)
                if "POD_NOT_FOUND" in message or "not found to terminate" in message.lower():
                    return True, "already absent"
                last_error = message
            else:
                return True, "terminated"
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            last_error = redact_text(exc)
        if attempt < retries:
            time.sleep(min(2**attempt, 4))
    return False, last_error


def cleanup_provider(
    node: Mapping[str, Any],
    mode: str,
    run_id: str,
    writer: ArtifactWriter,
) -> Dict[str, Any]:
    provider = node["provider"]
    cleanup = provider.get("cleanup")
    if not cleanup:
        result = {"node_id": node["id"], "provider_resource_id": provider["resource_id"], "status": "not-required"}
        writer.event("provider_cleanup", "skip", cleanup_status=result["status"], node_id=node["id"], provider_resource_id=provider["resource_id"])
        return result
    if mode == "mock":
        result = {"node_id": node["id"], "provider_resource_id": provider["resource_id"], "status": "simulated"}
        writer.event("provider_cleanup", "pass", cleanup_status=result["status"], node_id=node["id"], provider_resource_id=provider["resource_id"])
        return result
    if cleanup["kind"] == "runpod_api":
        started = time.monotonic()
        ok, detail = terminate_runpod(provider)
        result = {
            "node_id": node["id"],
            "provider_resource_id": provider["resource_id"],
            "status": "terminated" if ok else "manual-action-required",
            "detail": detail,
            "duration_seconds": round(time.monotonic() - started, 6),
        }
        writer.event(
            "provider_cleanup",
            "pass" if ok else "fail",
            **{("cleanup_status" if key == "status" else key): value for key, value in result.items()},
        )
        return result
    command_result = execute_command(
        {"kind": "local"},
        cleanup["command"],
        command_environment(run_id, writer.root, node),
        120,
    )
    meta = writer.command_result(str(node["id"]), "provider-cleanup", 1, command_result)
    result = {
        "node_id": node["id"],
        "provider_resource_id": provider["resource_id"],
        "status": "terminated" if command_result.returncode == 0 else "manual-action-required",
        **meta,
    }
    writer.event(
        "provider_cleanup",
        "pass" if command_result.returncode == 0 else "fail",
        **{("cleanup_status" if key == "status" else key): value for key, value in result.items()},
    )
    return result


def _parallel_node_stage(
    nodes: Sequence[Mapping[str, Any]],
    stage: str,
    mode: str,
    timeout_seconds: int,
    run_id: str,
    writer: ArtifactWriter,
) -> Dict[str, CommandResult]:
    out: Dict[str, CommandResult] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(nodes))) as pool:
        future_by_id = {
            pool.submit(run_node_stage, node, stage, mode, timeout_seconds, run_id, writer): node["id"]
            for node in nodes
        }
        for future in concurrent.futures.as_completed(future_by_id):
            node_id = future_by_id[future]
            out[node_id] = future.result()
    return out


def _parallel_readiness(
    inventory: Mapping[str, Any], mode: str, run_id: str, writer: ArtifactWriter
) -> Dict[str, Dict[str, Any]]:
    nodes = inventory["nodes"]
    out: Dict[str, Dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(nodes))) as pool:
        future_by_id = {
            pool.submit(probe_node, node, mode, inventory["readiness"], run_id, writer): node["id"]
            for node in nodes
        }
        errors = []
        for future in concurrent.futures.as_completed(future_by_id):
            node_id = future_by_id[future]
            try:
                out[node_id] = future.result()
            except Exception as exc:  # aggregate all nodes rather than hiding siblings
                errors.append(f"{node_id}: {exc}")
        if errors:
            raise ProofError("readiness failed: " + "; ".join(sorted(errors)))
    return out


def perform_cleanup(
    inventory: Mapping[str, Any],
    mode: str,
    run_id: str,
    writer: ArtifactWriter,
    run_workflow_cleanup: bool,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    if run_workflow_cleanup:
        if mode == "mock":
            rc = int(inventory["workflow"].get("mock", {}).get("cleanup_rc", 0))
            result = _mock_command_result(rc)
        elif inventory["workflow"].get("commands", {}).get("cleanup"):
            result = execute_command(
                {"kind": "local"},
                inventory["workflow"]["commands"]["cleanup"],
                command_environment(run_id, writer.root),
                120,
            )
        else:
            result = _mock_command_result(0)
            result.argv_kind = "not-configured"
        meta = writer.command_result("workflow", "cleanup", 1, result)
        row = {"scope": "workflow", "status": "clean" if result.returncode == 0 else "manual-action-required", **meta}
        results.append(row)
        writer.event(
            "workflow_cleanup",
            "pass" if result.returncode == 0 else "fail",
            **{("cleanup_status" if key == "status" else key): value for key, value in row.items()},
        )

    node_results = _parallel_node_stage(
        inventory["nodes"], "cleanup", mode, 120, run_id, writer
    )
    for node in inventory["nodes"]:
        result = node_results[node["id"]]
        results.append(
            {
                "scope": f"node:{node['id']}",
                "status": "clean" if result.returncode == 0 else "manual-action-required",
                "returncode": result.returncode,
            }
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(inventory["nodes"]))) as pool:
        future_by_id = {
            pool.submit(cleanup_provider, node, mode, run_id, writer): node["id"]
            for node in inventory["nodes"]
        }
        provider_by_id: Dict[str, Dict[str, Any]] = {}
        for future in concurrent.futures.as_completed(future_by_id):
            node_id = future_by_id[future]
            try:
                provider_by_id[node_id] = future.result()
            except Exception as exc:
                provider_by_id[node_id] = {
                    "node_id": node_id,
                    "status": "manual-action-required",
                    "detail": redact_text(exc),
                }
                writer.event(
                    "provider_cleanup",
                    "fail",
                    **{
                        ("cleanup_status" if key == "status" else key): value
                        for key, value in provider_by_id[node_id].items()
                    },
                )
        results.extend(provider_by_id[node["id"]] for node in inventory["nodes"])
    return results


def generated_run_id(prefix: str = "fleet") -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}"


def _git_commit() -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT),
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except OSError:
        return None


def run_proof(
    inventory: Dict[str, Any],
    mode: str,
    artifacts_base: Path = DEFAULT_ARTIFACTS,
    run_id: Optional[str] = None,
) -> Tuple[int, Path, Dict[str, Any]]:
    validate_inventory(inventory, mode)
    run_id = run_id or generated_run_id()
    if not SLUG_RE.fullmatch(run_id):
        raise ConfigError("run id must use only letters, digits, '.', '_' or '-'")
    writer = ArtifactWriter(artifacts_base / run_id, run_id)
    started_at = utc_now()
    inventory_digest = sha256_bytes(canonical_bytes(inventory))
    writer.write_json("inventory.snapshot.json", inventory)
    writer.event(
        "run_started",
        "planned" if mode == "dry-run" else "running",
        mode=mode,
        inventory_name=inventory["name"],
        inventory_sha256=inventory_digest,
        node_count=len(inventory["nodes"]),
    )
    summary: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "mode": mode,
        "inventory_name": inventory["name"],
        "inventory_sha256": inventory_digest,
        "git_commit": _git_commit(),
        "started_at": started_at,
        "status": "PLANNED" if mode == "dry-run" else "RUNNING",
        "assertions": [],
        "readiness": {},
        "submission": None,
        "receipts": [],
        "cleanup": [],
    }
    if mode == "dry-run":
        summary["finished_at"] = utc_now()
        writer.event("inventory_validated", "pass", requirements=inventory["requirements"])
        for node in inventory["nodes"]:
            writer.event(
                "node_planned",
                "planned",
                node_id=node["id"],
                substrate=node["substrate"],
                lane=node["lane"],
                provider_kind=node["provider"]["kind"],
                provider_resource_id=node["provider"]["resource_id"],
                transport=node["transport"]["kind"],
            )
        writer.event("run_finished", "planned")
        writer.write_json("summary.json", summary)
        writer.finalize_manifest()
        return 0, writer.root, summary

    failure: Optional[str] = None
    workflow_started = False
    assertions = Assertions()
    try:
        start_results = _parallel_node_stage(
            inventory["nodes"],
            "start",
            mode,
            int(inventory["readiness"]["command_timeout_seconds"]),
            run_id,
            writer,
        )
        bad_starts = {node_id: result.returncode for node_id, result in start_results.items() if result.returncode != 0}
        if bad_starts:
            raise ProofError(f"node start failure(s): {bad_starts}")

        readiness = _parallel_readiness(inventory, mode, run_id, writer)
        summary["readiness"] = readiness
        writer.write_json("readiness.json", readiness)
        assert_readiness(inventory, readiness, assertions)

        base_env = command_environment(
            run_id,
            writer.root,
            CX_FLEET_READINESS_JSON=str(writer.root / "readiness.json"),
        )
        workflow_started = True
        submit = run_workflow_command(
            inventory,
            "submit",
            mode,
            base_env,
            writer,
            timeout_seconds=int(inventory["collection"]["command_timeout_seconds"]),
        )
        summary["submission"] = submit
        writer.write_json("submission.json", submit)
        workload_id, job_ids = validate_submission(submit, run_id, assertions)
        collect_env = {
            **base_env,
            "CX_FLEET_SUBMISSION_JSON": str(writer.root / "submission.json"),
            "CX_FLEET_WORKLOAD_ID": workload_id,
            "CX_FLEET_JOB_IDS": ",".join(job_ids),
        }
        collected = run_workflow_command(
            inventory,
            "collect",
            mode,
            collect_env,
            writer,
            attempts=int(inventory["collection"]["attempts"]),
            interval=float(inventory["collection"]["poll_interval_seconds"]),
            timeout_seconds=int(inventory["collection"]["command_timeout_seconds"]),
        )
        writer.write_json("collected.json", collected)
        annotated = assert_receipts(inventory, readiness, submit, collected, run_id, assertions)
        summary["receipts"] = annotated
        summary["status"] = "PASS"
    except Exception as exc:
        failure = redact_text(exc)
        summary["status"] = "FAIL"
        summary["error"] = failure
        writer.event("proof_failed", "fail", error=failure)
    finally:
        try:
            cleanup = perform_cleanup(inventory, mode, run_id, writer, workflow_started)
            summary["cleanup"] = cleanup
            cleanup_failed = any(row.get("status") == "manual-action-required" for row in cleanup)
            if cleanup_failed:
                summary["status"] = "FAIL"
                summary["error"] = (summary.get("error", "") + "; cleanup requires manual action").strip("; ")
        except Exception as exc:
            summary["status"] = "FAIL"
            summary["error"] = (summary.get("error", "") + "; cleanup exception: " + redact_text(exc)).strip("; ")
            writer.event("cleanup_exception", "fail", error=redact_text(exc))

    summary["assertions"] = assertions.rows
    summary["finished_at"] = utc_now()
    for assertion in assertions.rows:
        writer.event(
            "assertion",
            "pass" if assertion["passed"] else "fail",
            assertion=assertion["name"],
            detail=assertion["detail"],
        )
    writer.event("run_finished", summary["status"].lower(), error=summary.get("error"))
    writer.write_json("summary.json", summary)
    writer.finalize_manifest()
    return (0 if summary["status"] == "PASS" else 1), writer.root, summary


def cleanup_only(
    inventory: Dict[str, Any],
    mode: str,
    artifacts_base: Path = DEFAULT_ARTIFACTS,
    run_id: Optional[str] = None,
) -> Tuple[int, Path, Dict[str, Any]]:
    if mode not in {"mock", "live"}:
        raise ConfigError("cleanup mode must be mock or live")
    validate_inventory(inventory, mode)
    run_id = run_id or generated_run_id("cleanup")
    writer = ArtifactWriter(artifacts_base / run_id, run_id)
    writer.write_json("inventory.snapshot.json", inventory)
    writer.event("cleanup_started", "running", mode=mode)
    cleanup = perform_cleanup(inventory, mode, run_id, writer, False)
    failed = any(row.get("status") == "manual-action-required" for row in cleanup)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "mode": mode,
        "status": "FAIL" if failed else "PASS",
        "cleanup": cleanup,
        "finished_at": utc_now(),
    }
    writer.event("cleanup_finished", summary["status"].lower())
    writer.write_json("summary.json", summary)
    writer.finalize_manifest()
    return (1 if failed else 0), writer.root, summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="validate inventory without executing commands")
    validate.add_argument("--inventory", required=True, type=Path)
    validate.add_argument("--mode", choices=("dry-run", "mock", "live"), default="dry-run")

    run = sub.add_parser("run", help="plan, mock, or execute a proof run")
    run.add_argument("--inventory", required=True, type=Path)
    run.add_argument("--mode", choices=("dry-run", "mock", "live"), default="dry-run")
    run.add_argument("--artifacts-dir", type=Path, default=DEFAULT_ARTIFACTS)
    run.add_argument("--run-id")

    cleanup = sub.add_parser("cleanup", help="stop inventoried agents and tear down ephemeral providers")
    cleanup.add_argument("--inventory", required=True, type=Path)
    cleanup.add_argument("--mode", choices=("mock", "live"), required=True)
    cleanup.add_argument("--artifacts-dir", type=Path, default=DEFAULT_ARTIFACTS)
    cleanup.add_argument("--run-id")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        inventory = load_inventory(args.inventory)
        if args.command == "validate":
            validate_inventory(inventory, args.mode)
            print(json.dumps({"ok": True, "mode": args.mode, "inventory": str(args.inventory), "nodes": len(inventory["nodes"])}, sort_keys=True))
            return 0
        if args.command == "run":
            if args.mode == "live":
                print(
                    "LIVE fleet proof: executing only the explicit inventory; no resources will be provisioned. "
                    "Ephemeral providers will be torn down on exit.",
                    file=sys.stderr,
                )
            rc, artifact_path, summary = run_proof(inventory, args.mode, args.artifacts_dir, args.run_id)
        else:
            rc, artifact_path, summary = cleanup_only(inventory, args.mode, args.artifacts_dir, args.run_id)
        print(json.dumps({"ok": rc == 0, "status": summary["status"], "run_id": summary["run_id"], "artifacts": str(artifact_path)}, sort_keys=True))
        return rc
    except (ConfigError, ProofError) as exc:
        print(f"ERROR: {redact_text(exc)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
