#!/usr/bin/env python3
"""Pinned resident Unix-socket worker for exact-request-reuse ABBA trials.

The process-per-arm control runner is intentionally conservative, but a
customer request to a deployed CX gateway does not cold-start Python.  This
worker lets the ABBA harness send each arm through one already-started local
service instead.  It does *not* make a cache lookup free: request framing,
identity validation, routing, cache lookup, target fallback, token delivery,
result/stage capture, and parent-side receipt validation remain inside every
timed arm.

Startup is recorded in an immutable session descriptor and is explicitly a
service lifecycle fact rather than a per-request result.  The descriptor is
local/unattested evidence only; it cannot authorize publication, billing, or a
fresh-decode claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import socketserver
import stat
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import cx_inference_exact_cache_runner_v1 as exact  # noqa: E402
import screen_inference_lane_abba as abba  # noqa: E402


SCHEMA_VERSION = 1
SESSION_KIND = "cx_inference_resident_exact_cache_session_v1"
REQUEST_KIND = "cx_inference_resident_exact_cache_execute_request_v1"
RESPONSE_KIND = "cx_inference_resident_exact_cache_execute_response_v1"
MAX_MESSAGE_BYTES = 8 << 20
SHA256_LENGTH = 64


class ResidentWorkerError(RuntimeError):
    """The resident worker/session cannot safely execute an arm."""


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ResidentWorkerError("canonical_json_failed") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def _object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ResidentWorkerError(f"duplicate_json_key_{key}")
        output[key] = value
    return output


def _reject_constant(value: str) -> None:
    raise ResidentWorkerError(f"nonfinite_json_constant_{value}")


def _exact(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ResidentWorkerError(f"{label}_must_be_object")
    missing = sorted(fields - set(value))
    unknown = sorted(set(value) - fields)
    if missing or unknown:
        raise ResidentWorkerError(
            f"{label}_fields_invalid_missing_{','.join(missing) or 'none'}"
            f"_unknown_{','.join(unknown) or 'none'}"
        )
    return value


def _string(value: Any, label: str, *, maximum: int = 16_384) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise ResidentWorkerError(f"{label}_must_be_nonempty_string")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ResidentWorkerError(f"{label}_must_be_integer_in_range")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != SHA256_LENGTH:
        raise ResidentWorkerError(f"{label}_must_be_sha256")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ResidentWorkerError(f"{label}_must_be_sha256") from exc
    return value.lower()


def _safe_regular(path_value: Path | str, label: str, *, maximum: int = MAX_MESSAGE_BYTES) -> tuple[Path, bytes]:
    path = Path(path_value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ResidentWorkerError(f"{label}_path_unsafe")
    try:
        before = path.stat()
        payload = path.read_bytes()
        after = path.stat()
    except OSError as exc:
        raise ResidentWorkerError(f"{label}_unreadable") from exc
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_size > maximum
        or identity_before != identity_after
    ):
        raise ResidentWorkerError(f"{label}_changed_or_invalid_size")
    try:
        # macOS exposes /var through /private/var.  Commands emitted by the
        # existing manifest builder are resolved, so the resident descriptor
        # must retain the same canonical spelling rather than compare an
        # equivalent but textual /var path as though it were a different pin.
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ResidentWorkerError(f"{label}_unresolvable") from exc
    return resolved, payload


def _read_json(path_value: Path | str, label: str) -> tuple[Path, dict[str, Any], bytes]:
    path, payload = _safe_regular(path_value, label)
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_object_no_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ResidentWorkerError) as exc:
        raise ResidentWorkerError(f"{label}_invalid_json") from exc
    if not isinstance(value, dict):
        raise ResidentWorkerError(f"{label}_root_must_be_object")
    return path, value, payload


def _new_absolute(path_value: Path | str, label: str) -> Path:
    path = Path(path_value)
    if (
        not path.is_absolute()
        or path.exists()
        or path.is_symlink()
        or path.parent.is_symlink()
        or not path.parent.is_dir()
    ):
        raise ResidentWorkerError(f"{label}_must_be_new_absolute_path")
    return path


def _write_new(path: Path, payload: bytes, label: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ResidentWorkerError(f"{label}_write_failed") from exc
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ResidentWorkerError(f"{label}_short_write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _pin(path_value: Path | str, role: str) -> dict[str, str]:
    path, payload = _safe_regular(path_value, f"session_{role}")
    return {"role": role, "path": str(path), "sha256": sha256_bytes(payload)}


def _revalidate_pin(value: Mapping[str, Any]) -> None:
    pin = _exact(value, {"role", "path", "sha256"}, "session_pin")
    role = _string(pin["role"], "session_pin.role", maximum=128)
    path, payload = _safe_regular(_string(pin["path"], "session_pin.path"), f"session_{role}")
    if str(path) != pin["path"] or sha256_bytes(payload) != _sha(pin["sha256"], "session_pin.sha256"):
        raise ResidentWorkerError("session_pinned_file_changed")


def _self_hash(value: Mapping[str, Any]) -> str:
    copied = dict(value)
    copied["session_sha256"] = ""
    return sha256_json(copied)


def _safe_work_root(path_value: Path | str) -> Path:
    path = Path(path_value)
    if (
        not path.is_absolute()
        or path.exists()
        or path.is_symlink()
        or path.parent.is_symlink()
        or not path.parent.is_dir()
    ):
        raise ResidentWorkerError("work_root_must_be_new_absolute_directory")
    try:
        return path.parent.resolve(strict=True) / path.name
    except OSError as exc:
        raise ResidentWorkerError("work_root_parent_unresolvable") from exc


def _expected_order(trials: int) -> list[str]:
    return [arm for pair in abba.repeatable_trial_orders(trials) for arm in pair]


def _pinned_file_by_role(pins: Sequence[Mapping[str, Any]], role: str) -> Path:
    matches = [pin for pin in pins if pin.get("role") == role]
    if len(matches) != 1:
        raise ResidentWorkerError(f"session_missing_or_duplicate_pin_{role}")
    return Path(_string(matches[0].get("path"), f"session_pin_{role}.path"))


def _parse_session(value: Any) -> dict[str, Any]:
    raw = _exact(
        value,
        {
            "schema_version",
            "kind",
            "claim_scope",
            "execution_transport",
            "lifecycle",
            "pid",
            "started_at_ns",
            "worker_epoch",
            "session_nonce",
            "socket_path",
            "work_root",
            "trials",
            "expected_arm_order",
            "pinned_files",
            "session_sha256",
        },
        "resident_session",
    )
    if raw["schema_version"] != SCHEMA_VERSION or raw["kind"] != SESSION_KIND:
        raise ResidentWorkerError("resident_session_identity_invalid")
    if raw["claim_scope"] != "physical_local_unattested_resident_service_binding_only":
        raise ResidentWorkerError("resident_session_claim_scope_invalid")
    if raw["execution_transport"] != "resident_unix_rpc":
        raise ResidentWorkerError("resident_session_transport_invalid")
    lifecycle = _exact(
        raw["lifecycle"],
        {"mode", "worker_startup_excluded", "cold_start_measured"},
        "resident_session.lifecycle",
    )
    if (
        lifecycle["mode"] != "resident_warm_service"
        or lifecycle["worker_startup_excluded"] is not True
        or lifecycle["cold_start_measured"] is not False
    ):
        raise ResidentWorkerError("resident_session_lifecycle_invalid")
    pid = _integer(raw["pid"], "resident_session.pid", minimum=1)
    started_at_ns = _integer(raw["started_at_ns"], "resident_session.started_at_ns", minimum=1)
    epoch = _string(raw["worker_epoch"], "resident_session.worker_epoch", maximum=128)
    nonce = _string(raw["session_nonce"], "resident_session.session_nonce", maximum=128)
    socket_path = Path(_string(raw["socket_path"], "resident_session.socket_path"))
    work_root = Path(_string(raw["work_root"], "resident_session.work_root"))
    if not socket_path.is_absolute() or not work_root.is_absolute():
        raise ResidentWorkerError("resident_session_paths_must_be_absolute")
    trials = _integer(raw["trials"], "resident_session.trials", minimum=1)
    expected = raw["expected_arm_order"]
    if not isinstance(expected, list) or expected != _expected_order(trials):
        raise ResidentWorkerError("resident_session_expected_order_invalid")
    pins = raw["pinned_files"]
    if not isinstance(pins, list) or not pins:
        raise ResidentWorkerError("resident_session_pins_invalid")
    parsed_pins = [_exact(pin, {"role", "path", "sha256"}, "resident_session.pin") for pin in pins]
    roles = [str(pin["role"]) for pin in parsed_pins]
    required = {
        "resident_worker_source",
        "exact_cache_runner_source",
        "baseline_manifest",
        "candidate_manifest",
        "workload",
        "endpoint_attestation",
        "exact_response_cache",
    }
    if set(roles) != required or len(set(roles)) != len(roles):
        raise ResidentWorkerError("resident_session_pin_roles_invalid")
    supplied_sha = _sha(raw["session_sha256"], "resident_session.session_sha256")
    if supplied_sha != _self_hash(raw):
        raise ResidentWorkerError("resident_session_self_hash_invalid")
    return {
        "value": raw,
        "pid": pid,
        "started_at_ns": started_at_ns,
        "worker_epoch": epoch,
        "session_nonce": nonce,
        "socket_path": socket_path,
        "work_root": work_root,
        "trials": trials,
        "expected_arm_order": tuple(expected),
        "pins": tuple(parsed_pins),
        "session_sha256": supplied_sha,
    }


def load_session(path_value: Path | str, *, require_socket: bool) -> dict[str, Any]:
    _path, raw, _payload = _read_json(path_value, "resident_session")
    session = _parse_session(raw)
    for pin in session["pins"]:
        _revalidate_pin(pin)
    if require_socket:
        socket_path = session["socket_path"]
        if socket_path.is_symlink() or not socket_path.exists() or not stat.S_ISSOCK(socket_path.stat().st_mode):
            raise ResidentWorkerError("resident_session_socket_not_live")
    return session


def _load_prepared_inputs(
    *,
    baseline_manifest: Path,
    candidate_manifest: Path,
    workload_path: Path,
    endpoint_attestation_path: Path,
    cache_path: Path,
) -> None:
    """Fail before serving if the frozen exact-cache route is not executable."""

    try:
        baseline = abba.load_arm_manifest(baseline_manifest, "baseline")
        candidate = abba.load_arm_manifest(candidate_manifest, "candidate")
        if baseline.logical_work.value != candidate.logical_work.value:
            raise ResidentWorkerError("resident_baseline_candidate_logical_work_mismatch")
        workload = exact.load_workload(workload_path, runtime=baseline.runtime)
        exact._validate_endpoint_attestation(
            endpoint_attestation_path, workload=workload, runtime=baseline.runtime
        )
        exact._validate_endpoint_attestation(
            endpoint_attestation_path, workload=workload, runtime=candidate.runtime
        )
        cache = exact.load_cache(cache_path, workload=workload)
    except (abba.InferenceLaneContractError, exact.ExactCacheRunnerError) as exc:
        raise ResidentWorkerError("resident_prepared_input_invalid") from exc
    if cache["history_sha256"] != candidate.cache_policy["cache_history_sha256"]:
        raise ResidentWorkerError("resident_cache_history_differs_from_candidate_manifest")


def build_session(
    *,
    baseline_manifest: Path,
    candidate_manifest: Path,
    workload_path: Path,
    endpoint_attestation_path: Path,
    cache_path: Path,
    work_root: Path,
    trials: int,
    socket_path: Path,
) -> dict[str, Any]:
    baseline_manifest = _safe_regular(baseline_manifest, "baseline_manifest")[0]
    candidate_manifest = _safe_regular(candidate_manifest, "candidate_manifest")[0]
    workload_path = _safe_regular(workload_path, "workload")[0]
    endpoint_attestation_path = _safe_regular(endpoint_attestation_path, "endpoint_attestation")[0]
    cache_path = _safe_regular(cache_path, "exact_response_cache")[0]
    work_root = _safe_work_root(work_root)
    if not socket_path.is_absolute() or socket_path.exists() or socket_path.is_symlink() or socket_path.parent.is_symlink() or not socket_path.parent.is_dir():
        raise ResidentWorkerError("socket_path_must_be_new_absolute_path")
    if trials < abba.MIN_TRIALS:
        raise ResidentWorkerError("resident_trials_below_strict_minimum")
    _load_prepared_inputs(
        baseline_manifest=baseline_manifest,
        candidate_manifest=candidate_manifest,
        workload_path=workload_path,
        endpoint_attestation_path=endpoint_attestation_path,
        cache_path=cache_path,
    )
    pins = [
        _pin(Path(__file__).resolve(), "resident_worker_source"),
        _pin(Path(exact.__file__).resolve(), "exact_cache_runner_source"),
        _pin(baseline_manifest, "baseline_manifest"),
        _pin(candidate_manifest, "candidate_manifest"),
        _pin(workload_path, "workload"),
        _pin(endpoint_attestation_path, "endpoint_attestation"),
        _pin(cache_path, "exact_response_cache"),
    ]
    value: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": SESSION_KIND,
        "claim_scope": "physical_local_unattested_resident_service_binding_only",
        "execution_transport": "resident_unix_rpc",
        "lifecycle": {
            "mode": "resident_warm_service",
            "worker_startup_excluded": True,
            "cold_start_measured": False,
        },
        "pid": os.getpid(),
        "started_at_ns": time.time_ns(),
        "worker_epoch": secrets.token_hex(16),
        "session_nonce": secrets.token_hex(32),
        "socket_path": str(socket_path),
        "work_root": str(work_root),
        "trials": trials,
        "expected_arm_order": _expected_order(trials),
        "pinned_files": pins,
        "session_sha256": "",
    }
    value["session_sha256"] = _self_hash(value)
    return value


def _output_paths(args: argparse.Namespace, *, expected_trial_dir: Path) -> None:
    result = Path(args.result_out)
    if result != expected_trial_dir / "arm-result.json":
        raise ResidentWorkerError("resident_result_path_not_expected_trial_output")
    for stage in abba.STAGES:
        path = Path(getattr(args, f"stage_{stage}_out"))
        if path != expected_trial_dir / f"{stage}.json":
            raise ResidentWorkerError("resident_stage_path_not_expected_trial_output")


class ResidentExecution:
    """Serial executor that owns one frozen worker session."""

    def __init__(self, session_path: Path, session: Mapping[str, Any]) -> None:
        self.session_path = session_path
        self.session = _parse_session(session)
        pins = self.session["pins"]
        self.baseline_manifest = _pinned_file_by_role(pins, "baseline_manifest")
        self.candidate_manifest = _pinned_file_by_role(pins, "candidate_manifest")
        self.workload_path = _pinned_file_by_role(pins, "workload")
        self.endpoint_attestation_path = _pinned_file_by_role(pins, "endpoint_attestation")
        self.cache_path = _pinned_file_by_role(pins, "exact_response_cache")
        self.baseline = abba.load_arm_manifest(self.baseline_manifest, "baseline")
        self.candidate = abba.load_arm_manifest(self.candidate_manifest, "candidate")
        self.ordinal = 0
        self.failed = False

    def _revalidate(self) -> None:
        current = load_session(self.session_path, require_socket=True)
        if current["session_sha256"] != self.session["session_sha256"]:
            raise ResidentWorkerError("resident_session_changed_after_start")

    def _parse_command(self, command: Sequence[str], *, expected_arm: str, ordinal: int) -> argparse.Namespace:
        if not isinstance(command, Sequence) or len(command) < 3 or not all(isinstance(item, str) and item for item in command):
            raise ResidentWorkerError("resident_command_invalid")
        arm = self.baseline if expected_arm == "baseline" else self.candidate
        trial_index = ordinal // 2
        expected_trial_dir = self.session["work_root"] / f"trial-{trial_index:04d}" / expected_arm
        expected = tuple(item.replace("{trial_dir}", str(expected_trial_dir)) for item in arm.command)
        if tuple(command) != expected:
            raise ResidentWorkerError("resident_command_differs_from_pinned_manifest")
        try:
            # The manifest command begins with the executable runner script,
            # followed by its normal ``run`` subcommand; it does not require
            # a separate Python-interpreter argument.
            parsed = exact.parse_args(list(command[1:]))
        except SystemExit as exc:
            raise ResidentWorkerError("resident_command_parse_failed") from exc
        if parsed.command != "run" or parsed.arm != expected_arm:
            raise ResidentWorkerError("resident_command_arm_invalid")
        expected_manifest = self.baseline_manifest if expected_arm == "baseline" else self.candidate_manifest
        try:
            arm_manifest_path = Path(parsed.arm_manifest).resolve(strict=True)
            workload_path = Path(parsed.workload).resolve(strict=True)
            endpoint_attestation_path = Path(parsed.endpoint_attestation).resolve(strict=True)
        except OSError as exc:
            raise ResidentWorkerError("resident_command_binding_path_unresolvable") from exc
        if (
            arm_manifest_path != expected_manifest
            or workload_path != self.workload_path
            or endpoint_attestation_path != self.endpoint_attestation_path
        ):
            raise ResidentWorkerError("resident_command_binding_invalid")
        if expected_arm == "candidate":
            try:
                cache_path = Path(parsed.cache_artifact).resolve(strict=True)
            except OSError as exc:
                raise ResidentWorkerError("resident_candidate_cache_path_unresolvable") from exc
            if cache_path != self.cache_path:
                raise ResidentWorkerError("resident_candidate_cache_binding_invalid")
        elif parsed.cache_artifact is not None:
            raise ResidentWorkerError("resident_baseline_cache_forbidden")
        _output_paths(parsed, expected_trial_dir=expected_trial_dir)
        return parsed

    def execute(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if self.failed:
            raise ResidentWorkerError("resident_session_failed_closed")
        try:
            raw = _exact(
                request,
                {"schema_version", "kind", "session_nonce", "ordinal", "command"},
                "resident_execute_request",
            )
            if raw["schema_version"] != SCHEMA_VERSION or raw["kind"] != REQUEST_KIND:
                raise ResidentWorkerError("resident_request_identity_invalid")
            if _string(raw["session_nonce"], "resident_request.nonce") != self.session["session_nonce"]:
                raise ResidentWorkerError("resident_request_nonce_invalid")
            ordinal = _integer(raw["ordinal"], "resident_request.ordinal", minimum=0)
            if ordinal != self.ordinal or ordinal >= len(self.session["expected_arm_order"]):
                raise ResidentWorkerError("resident_request_sequence_invalid")
            command_raw = raw["command"]
            if not isinstance(command_raw, list):
                raise ResidentWorkerError("resident_request_command_invalid")
            command = tuple(_string(item, "resident_request.command_item") for item in command_raw)
            self._revalidate()
            expected_arm = self.session["expected_arm_order"][ordinal]
            parsed = self._parse_command(command, expected_arm=expected_arm, ordinal=ordinal)
            run = exact.run_arm(parsed, actual_argv=command)
            result_path, result_bytes = _safe_regular(parsed.result_out, "resident_result", maximum=MAX_MESSAGE_BYTES)
            try:
                result = json.loads(
                    result_bytes.decode("utf-8"),
                    object_pairs_hook=_object_no_duplicates,
                    parse_constant=_reject_constant,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ResidentWorkerError) as exc:
                raise ResidentWorkerError("resident_result_invalid_json") from exc
            if not isinstance(result, dict):
                raise ResidentWorkerError("resident_result_invalid_root")
            self._revalidate()
            self.ordinal += 1
            response: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "kind": RESPONSE_KIND,
                "status": "ok",
                "ordinal": ordinal,
                "worker_epoch": self.session["worker_epoch"],
                "session_sha256": self.session["session_sha256"],
                "command_sha256": sha256_json(list(command)),
                "result_path": str(result_path),
                "result_sha256": sha256_bytes(result_bytes),
                # Returning the complete token-ID result makes this a real
                # worker-to-caller delivery path rather than a shared-file
                # timing shortcut.  The parent cross-checks it against disk.
                "result": result,
                "run": run,
            }
            response["response_sha256"] = sha256_json(response)
            return response
        except BaseException:
            self.failed = True
            raise


class _UnixRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        worker: _ResidentUnixServer = self.server  # type: ignore[assignment]
        raw = self.rfile.readline(MAX_MESSAGE_BYTES + 1)
        if not raw or len(raw) > MAX_MESSAGE_BYTES or not raw.endswith(b"\n"):
            self._send({"status": "FAIL", "error": "resident_request_frame_invalid"})
            return
        try:
            value = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_object_no_duplicates,
                parse_constant=_reject_constant,
            )
            if not isinstance(value, dict):
                raise ResidentWorkerError("resident_request_root_invalid")
            response = worker.execution.execute(value)
        except Exception as exc:  # noqa: BLE001 - protocol boundary fails closed.
            response = {"status": "FAIL", "error": str(exc) if isinstance(exc, ResidentWorkerError) else "resident_worker_internal_error"}
        self._send(response)

    def _send(self, value: Mapping[str, Any]) -> None:
        payload = canonical_json_bytes(value) + b"\n"
        if len(payload) > MAX_MESSAGE_BYTES:
            payload = canonical_json_bytes({"status": "FAIL", "error": "resident_response_too_large"}) + b"\n"
        self.wfile.write(payload)
        self.wfile.flush()


class _ResidentUnixServer(socketserver.UnixStreamServer):
    def __init__(self, socket_path: str, execution: ResidentExecution) -> None:
        self.execution = execution
        super().__init__(socket_path, _UnixRequestHandler)


class ResidentRpcRunner:
    """``subprocess.run``-shaped client used by the generic ABBA harness."""

    def __init__(self, session_path: Path) -> None:
        self.session_path = Path(session_path)
        self.session = load_session(self.session_path, require_socket=True)
        self.ordinal = 0
        self._metadata: dict[str, dict[str, Any]] = {}

    def _refresh(self) -> dict[str, Any]:
        session = load_session(self.session_path, require_socket=True)
        if session["session_sha256"] != self.session["session_sha256"]:
            raise ResidentWorkerError("resident_session_changed_after_client_start")
        return session

    def __call__(
        self,
        args: Sequence[str],
        *,
        cwd: str | None = None,
        stdout: Any = None,
        stderr: Any = None,
        timeout: float | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[bytes]:
        del cwd, stdout, stderr, check
        session = self._refresh()
        if self.ordinal >= len(session["expected_arm_order"]):
            raise ResidentWorkerError("resident_client_sequence_exhausted")
        command = tuple(str(item) for item in args)
        request = {
            "schema_version": SCHEMA_VERSION,
            "kind": REQUEST_KIND,
            "session_nonce": session["session_nonce"],
            "ordinal": self.ordinal,
            "command": list(command),
        }
        request_bytes = canonical_json_bytes(request) + b"\n"
        if len(request_bytes) > MAX_MESSAGE_BYTES:
            raise ResidentWorkerError("resident_client_request_too_large")
        socket_timeout = float(timeout) if timeout is not None else 600.0
        if socket_timeout <= 0:
            raise ResidentWorkerError("resident_client_timeout_invalid")
        response_bytes = b""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(socket_timeout)
                client.connect(str(session["socket_path"]))
                client.sendall(request_bytes)
                while b"\n" not in response_bytes:
                    chunk = client.recv(min(65_536, MAX_MESSAGE_BYTES + 1 - len(response_bytes)))
                    if not chunk:
                        break
                    response_bytes += chunk
                    if len(response_bytes) > MAX_MESSAGE_BYTES:
                        raise ResidentWorkerError("resident_client_response_too_large")
        except (OSError, TimeoutError) as exc:
            raise ResidentWorkerError("resident_client_transport_failed") from exc
        if not response_bytes.endswith(b"\n"):
            raise ResidentWorkerError("resident_client_response_frame_invalid")
        try:
            response = json.loads(
                response_bytes[:-1].decode("utf-8"),
                object_pairs_hook=_object_no_duplicates,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ResidentWorkerError) as exc:
            raise ResidentWorkerError("resident_client_response_invalid_json") from exc
        if not isinstance(response, dict) or response.get("status") != "ok":
            error = response.get("error") if isinstance(response, dict) else None
            if isinstance(error, str) and error:
                raise ResidentWorkerError(f"resident_worker_rejected_{error}")
            raise ResidentWorkerError("resident_worker_rejected_invalid_response")
        expected = _exact(
            response,
            {
                "schema_version",
                "kind",
                "status",
                "ordinal",
                "worker_epoch",
                "session_sha256",
                "command_sha256",
                "result_path",
                "result_sha256",
                "result",
                "run",
                "response_sha256",
            },
            "resident_client_response",
        )
        supplied_response_sha = _sha(expected["response_sha256"], "resident_client.response_sha256")
        hashable = dict(expected)
        hashable.pop("response_sha256")
        if supplied_response_sha != sha256_json(hashable):
            raise ResidentWorkerError("resident_client_response_self_hash_invalid")
        if (
            expected["schema_version"] != SCHEMA_VERSION
            or expected["kind"] != RESPONSE_KIND
            or expected["ordinal"] != self.ordinal
            or expected["worker_epoch"] != session["worker_epoch"]
            or expected["session_sha256"] != session["session_sha256"]
            or expected["command_sha256"] != sha256_json(list(command))
        ):
            raise ResidentWorkerError("resident_client_response_binding_invalid")
        result_path, result_bytes = _safe_regular(
            _string(expected["result_path"], "resident_client.result_path"),
            "resident_client_result",
        )
        if sha256_bytes(result_bytes) != _sha(expected["result_sha256"], "resident_client.result_sha256"):
            raise ResidentWorkerError("resident_client_result_changed")
        if canonical_json_bytes(expected["result"]) + b"\n" != result_bytes:
            raise ResidentWorkerError("resident_client_delivered_result_differs_from_capture")
        command_sha = sha256_json(list(command))
        self._metadata[command_sha] = {
            "transport": "resident_unix_rpc",
            "session_path": str(self.session_path),
            "session_sha256": session["session_sha256"],
            "worker_epoch": session["worker_epoch"],
            "request_sha256": sha256_bytes(request_bytes),
            "response_sha256": supplied_response_sha,
            "result_path": str(result_path),
        }
        self.ordinal += 1
        self._refresh()
        return subprocess.CompletedProcess(list(command), 0, b"", b"")

    def metadata_for(self, args: Sequence[str]) -> Mapping[str, Any] | None:
        return self._metadata.get(sha256_json(list(args)))

    def context(self) -> dict[str, Any]:
        session = self._refresh()
        return {
            "execution_transport": "resident_unix_rpc",
            "session_path": str(self.session_path),
            "session_sha256": session["session_sha256"],
            "worker_epoch": session["worker_epoch"],
            "lifecycle": dict(session["value"]["lifecycle"]),
        }


def serve(args: argparse.Namespace) -> int:
    session_out = _new_absolute(args.session_out, "session_out")
    socket_path = Path(args.socket_path)
    session_value = build_session(
        baseline_manifest=args.baseline_manifest,
        candidate_manifest=args.candidate_manifest,
        workload_path=args.workload,
        endpoint_attestation_path=args.endpoint_attestation,
        cache_path=args.cache_artifact,
        work_root=args.work_root,
        trials=args.trials,
        socket_path=socket_path,
    )
    execution = ResidentExecution(session_out, session_value)
    server = _ResidentUnixServer(str(socket_path), execution)
    try:
        os.chmod(socket_path, 0o600)
        _write_new(session_out, canonical_json_bytes(session_value) + b"\n", "session_out")
        print(
            json.dumps(
                {
                    "session": str(session_out),
                    "session_sha256": session_value["session_sha256"],
                    "socket_path": str(socket_path),
                    "claim": "resident worker startup only; no arm timing or performance claim",
                },
                sort_keys=True,
            ),
            flush=True,
        )
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        socket_path.unlink(missing_ok=True)
    return 0


def _positive_int(value: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must_be_integer") from exc
    if parsed < abba.MIN_TRIALS:
        raise argparse.ArgumentTypeError(f"must_be_at_least_{abba.MIN_TRIALS}")
    return parsed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve_parser = sub.add_parser("serve")
    serve_parser.add_argument("--baseline-manifest", type=Path, required=True)
    serve_parser.add_argument("--candidate-manifest", type=Path, required=True)
    serve_parser.add_argument("--workload", type=Path, required=True)
    serve_parser.add_argument("--endpoint-attestation", type=Path, required=True)
    serve_parser.add_argument("--cache-artifact", type=Path, required=True)
    serve_parser.add_argument("--work-root", type=Path, required=True)
    serve_parser.add_argument("--trials", type=_positive_int, required=True)
    serve_parser.add_argument("--socket-path", type=Path, required=True)
    serve_parser.add_argument("--session-out", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command != "serve":  # pragma: no cover - argparse fixes this.
            raise ResidentWorkerError("resident_command_unknown")
        return serve(args)
    except ResidentWorkerError as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
