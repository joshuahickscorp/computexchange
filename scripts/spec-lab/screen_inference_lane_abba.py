#!/usr/bin/env python3
"""Fail-closed, whole-request ABBA measurement contract for inference lanes.

This is deliberately a *measurement boundary*, not an inference engine.  A
caller supplies two pinned arm manifests for exactly one lane:

* ``exact_request_reuse`` -- an already-produced identical request may be
  returned from an exact-response cache;
* ``shared_prefix_reuse`` -- a new completion may start from a declared shared
  prefix; and
* ``fresh_decode`` -- neither response nor prefix reuse is permitted.

The contract measures one wall-clock boundary per arm, from immediately before
the pinned command invocation through result, stage-receipt, output-parity,
and pin validation.  It requires ABBA blocks of four and at least eight
samples per arm for a qualifying receipt, exact completion token IDs,
predeclared reuse eligibility, a bound model/tokenizer/runtime/cache history,
and a validated direct-target fallback declaration.  When
``--qualifying-receipt-out`` is supplied, the runner adapts the completed raw
ABBA evidence into the strict scorecard receipt format and retains its
immutable artifacts under the new work root.

It intentionally never adds stage ratios, mixes lanes, or computes a
portfolio/aggregate multiplier.  A resulting receipt is raw local evidence;
separate scorecard and authorization work is still required before any public
or production claim.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
import statistics
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, Sequence

import cx_inference_receipt_v1 as strict_receipt


SCHEMA_VERSION = 1
KIND = "cx_inference_lane_abba_measurement_receipt_v1"
ARM_MANIFEST_KIND = "cx_inference_lane_abba_arm_manifest_v1"
ARM_RESULT_KIND = "cx_inference_lane_abba_arm_result_v1"
STAGE_RECEIPT_KIND = "cx_inference_lane_abba_stage_receipt_v1"
LANES = ("exact_request_reuse", "shared_prefix_reuse", "fresh_decode")
STAGES = (
    "admission",
    "routing",
    "cache_lookup",
    "engine_execution",
    "verification",
    "serialization",
    "delivery",
)
ABBA_ORDERS = (
    ("baseline", "candidate"),
    ("candidate", "baseline"),
    ("baseline", "candidate"),
    ("candidate", "baseline"),
)
# A trial is one ordered baseline/candidate pair. Eight trial pairs therefore
# retain the strict receipt minimum of eight independent samples per arm.
MIN_TRIALS = strict_receipt.MIN_TRIALS_PER_ARM
MAX_TRIALS = 32
MAX_REQUESTS = 4_096
MAX_OUTPUT_TOKENS = 32_768
MAX_MANIFEST_BYTES = 4 << 20
MAX_RECEIPT_BYTES = 64 << 20
MAX_COMMAND_ARGUMENTS = 128
MAX_TIMEOUT_SECONDS = 7_200
DEFAULT_TARGET_MULTIPLIER = 50.0


class InferenceLaneContractError(RuntimeError):
    """A requested measurement cannot provide trustworthy, same-work evidence."""


def _target_multiplier(value: Any) -> float:
    """Accept a declared lane target without silently changing the 50× default."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InferenceLaneContractError("target_multiplier_must_be_a_finite_number")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 1.0:
        raise InferenceLaneContractError("target_multiplier_must_be_at_least_one")
    return parsed


Clock = Callable[[], int]
Runner = Callable[..., subprocess.CompletedProcess[bytes]]


@dataclass(frozen=True)
class FilePin:
    path: Path
    sha256: str
    identity: tuple[int, int, int, int, int, int]


@dataclass(frozen=True)
class LogicalWork:
    value: Mapping[str, Any]
    sha256: str
    lane: str
    request_digests: tuple[str, ...]
    eligible_indexes: tuple[int, ...]


@dataclass(frozen=True)
class ArmManifest:
    name: str
    manifest_pin: FilePin
    logical_work: LogicalWork
    runtime: Mapping[str, Any]
    runtime_sha256: str
    cache_policy: Mapping[str, Any]
    cache_policy_sha256: str
    authorization: Mapping[str, bool]
    command: tuple[str, ...]
    command_pins: tuple[FilePin, ...]
    timeout_secs: int
    result_template: PurePosixPath
    stage_templates: Mapping[str, PurePosixPath]


@dataclass(frozen=True)
class RunConfig:
    baseline_manifest: Path
    candidate_manifest: Path
    work_root: Path
    receipt_out: Path
    trials: int
    qualifying_receipt_out: Path | None = None
    resident_session: Path | None = None
    target_multiplier: float = DEFAULT_TARGET_MULTIPLIER

    def validate(self) -> None:
        if type(self.trials) is not int or not (
            MIN_TRIALS <= self.trials <= MAX_TRIALS and self.trials % len(ABBA_ORDERS) == 0
        ):
            raise InferenceLaneContractError("trials_must_be_a_positive_abba_block")
        _target_multiplier(self.target_multiplier)
        for path, label in (
            (self.baseline_manifest, "baseline_manifest"),
            (self.candidate_manifest, "candidate_manifest"),
        ):
            if not path.is_absolute() or path.is_symlink() or not path.is_file():
                raise InferenceLaneContractError(f"{label}_unsafe")
        if self.baseline_manifest.resolve(strict=True) == self.candidate_manifest.resolve(strict=True):
            raise InferenceLaneContractError("baseline_and_candidate_manifests_must_be_distinct")
        paths: list[tuple[Path, str]] = [
            (self.work_root, "work_root"),
            (self.receipt_out, "receipt_out"),
        ]
        if self.qualifying_receipt_out is not None:
            if self.qualifying_receipt_out == self.receipt_out:
                raise InferenceLaneContractError(
                    "qualifying_receipt_out_must_differ_from_measurement_receipt"
                )
            paths.append((self.qualifying_receipt_out, "qualifying_receipt_out"))
        if self.resident_session is not None:
            if (
                not self.resident_session.is_absolute()
                or self.resident_session.is_symlink()
                or not self.resident_session.is_file()
            ):
                raise InferenceLaneContractError("resident_session_unsafe")
        for path, label in paths:
            if not path.is_absolute() or path.exists() or path.is_symlink():
                raise InferenceLaneContractError(f"{label}_must_be_a_new_absolute_path")
            if path.parent.is_symlink() or not path.parent.is_dir():
                raise InferenceLaneContractError(f"{label}_parent_unsafe")


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise InferenceLaneContractError("noncanonical_json_value") from exc


def sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _sha(value: Any, label: str) -> str:
    if not _is_sha256(value):
        raise InferenceLaneContractError(f"{label}_must_be_a_lowercase_sha256")
    return value


def _sha_or_null(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _sha(value, label)


def _string(value: Any, label: str, *, maximum: int = 1_024) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise InferenceLaneContractError(f"{label}_must_be_a_nonempty_string")
    return value


def _bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise InferenceLaneContractError(f"{label}_must_be_boolean")
    return value


def _int(value: Any, label: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise InferenceLaneContractError(f"{label}_must_be_an_integer_in_range")
    return value


def _exact_keys(value: Any, keys: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise InferenceLaneContractError(f"{label}_must_be_an_object")
    missing = sorted(keys - set(value))
    unknown = sorted(set(value) - keys)
    if missing or unknown:
        raise InferenceLaneContractError(
            f"{label}_fields_invalid_missing_{','.join(missing) or 'none'}_unknown_{','.join(unknown) or 'none'}"
        )
    return value


def _duplicate_safe_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InferenceLaneContractError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise InferenceLaneContractError(f"nonfinite_json_constant_{value}")


def _identity(path: Path) -> tuple[int, int, int, int, int, int]:
    try:
        info = path.stat()
    except OSError as exc:
        raise InferenceLaneContractError("file_stat_failed") from exc
    if not stat.S_ISREG(info.st_mode):
        raise InferenceLaneContractError("path_is_not_a_regular_file")
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _sha256_file(path: Path, *, maximum: int | None) -> str:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                total += len(chunk)
                if maximum is not None and total > maximum:
                    raise InferenceLaneContractError("file_exceeds_size_limit")
                digest.update(chunk)
    except OSError as exc:
        raise InferenceLaneContractError("file_hash_failed") from exc
    return digest.hexdigest()


def _pin_file(path_value: Any, expected_sha256: Any, label: str, *, executable: bool = False) -> FilePin:
    if not isinstance(path_value, str):
        raise InferenceLaneContractError(f"{label}_path_invalid")
    path = Path(path_value)
    if not path.is_absolute() or path.is_symlink():
        raise InferenceLaneContractError(f"{label}_path_unsafe")
    try:
        canonical = path.resolve(strict=True)
    except OSError as exc:
        raise InferenceLaneContractError(f"{label}_unavailable") from exc
    identity = _identity(canonical)
    if executable and not os.access(canonical, os.X_OK):
        raise InferenceLaneContractError(f"{label}_not_executable")
    digest = _sha256_file(canonical, maximum=None)
    if digest != _sha(expected_sha256, f"{label}_sha256"):
        raise InferenceLaneContractError(f"{label}_sha256_mismatch")
    if _identity(canonical) != identity:
        raise InferenceLaneContractError(f"{label}_changed_while_pinning")
    return FilePin(path=canonical, sha256=digest, identity=identity)


def _revalidate_pin(pin: FilePin, label: str) -> None:
    if _identity(pin.path) != pin.identity or _sha256_file(pin.path, maximum=None) != pin.sha256:
        raise InferenceLaneContractError(f"pinned_{label}_changed")


def _read_json(path: Path, label: str, *, maximum: int = MAX_RECEIPT_BYTES) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise InferenceLaneContractError(f"{label}_not_a_regular_file")
    before = _identity(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise InferenceLaneContractError(f"{label}_unreadable") from exc
    if not 1 <= len(raw) <= maximum or _identity(path) != before:
        raise InferenceLaneContractError(f"{label}_changed_or_size_invalid")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_duplicate_safe_object, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, InferenceLaneContractError) as exc:
        raise InferenceLaneContractError(f"{label}_invalid_json") from exc
    if not isinstance(value, dict):
        raise InferenceLaneContractError(f"{label}_root_must_be_object")
    return value, raw


def _revision(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise InferenceLaneContractError(f"{label}_must_be_a_pinned_40_char_revision")
    return value


def _parse_logical_work(value: Any, lane: str) -> LogicalWork:
    raw = _exact_keys(
        value,
        {
            "model",
            "corpus_sha256",
            "request_digests",
            "request_order_sha256",
            "input_token_ids_sha256",
            "sampling_contract_sha256",
            "sampling",
            "max_output_tokens",
            "concurrency",
            "reuse_contract",
        },
        "logical_work",
    )
    model = _exact_keys(raw["model"], {"model_id", "model_revision", "tokenizer_id", "tokenizer_revision"}, "logical_work_model")
    _string(model["model_id"], "logical_work_model_id")
    _revision(model["model_revision"], "logical_work_model_revision")
    _string(model["tokenizer_id"], "logical_work_tokenizer_id")
    _revision(model["tokenizer_revision"], "logical_work_tokenizer_revision")
    _sha(raw["corpus_sha256"], "logical_work_corpus_sha256")
    request_values = raw["request_digests"]
    if not isinstance(request_values, list) or not 1 <= len(request_values) <= MAX_REQUESTS:
        raise InferenceLaneContractError("logical_work_request_digests_invalid")
    request_digests = tuple(_sha(item, f"logical_work_request_digest_{index}") for index, item in enumerate(request_values))
    if _sha(raw["request_order_sha256"], "logical_work_request_order_sha256") != sha256_json(list(request_digests)):
        raise InferenceLaneContractError("logical_work_request_order_digest_mismatch")
    _sha(raw["input_token_ids_sha256"], "logical_work_input_token_ids_sha256")
    sampling = _exact_keys(
        raw["sampling"],
        {"temperature", "top_p", "seed", "n"},
        "logical_work_sampling",
    )
    if type(sampling["temperature"]) not in {int, float} or sampling["temperature"] != 0:
        raise InferenceLaneContractError("logical_work_sampling_temperature_must_be_zero")
    if type(sampling["top_p"]) not in {int, float} or sampling["top_p"] != 1:
        raise InferenceLaneContractError("logical_work_sampling_top_p_must_be_one")
    _int(sampling["seed"], "logical_work_sampling_seed", minimum=0)
    if _int(sampling["n"], "logical_work_sampling_n", minimum=1, maximum=1) != 1:
        raise InferenceLaneContractError("logical_work_sampling_n_must_be_one")
    stable_sampling = json.loads(_canonical_json(sampling))
    if _sha(raw["sampling_contract_sha256"], "logical_work_sampling_contract_sha256") != sha256_json(
        stable_sampling
    ):
        raise InferenceLaneContractError("logical_work_sampling_contract_digest_mismatch")
    _int(raw["max_output_tokens"], "logical_work_max_output_tokens", minimum=1, maximum=MAX_OUTPUT_TOKENS)
    _int(raw["concurrency"], "logical_work_concurrency", minimum=1, maximum=MAX_REQUESTS)
    reuse = _exact_keys(
        raw["reuse_contract"],
        {
            "eligible_request_indexes",
            "exact_request_key_schema_sha256",
            "shared_prefix_token_ids_sha256",
            "shared_prefix_token_count",
            "required_eligible_hit_rate",
        },
        "logical_work_reuse_contract",
    )
    raw_indexes = reuse["eligible_request_indexes"]
    if not isinstance(raw_indexes, list):
        raise InferenceLaneContractError("logical_work_eligible_request_indexes_invalid")
    indexes = tuple(_int(item, f"logical_work_eligible_index_{index}", minimum=0, maximum=len(request_digests) - 1) for index, item in enumerate(raw_indexes))
    if tuple(sorted(set(indexes))) != indexes:
        raise InferenceLaneContractError("logical_work_eligible_indexes_must_be_sorted_unique")
    exact_key = _sha_or_null(reuse["exact_request_key_schema_sha256"], "logical_work_exact_key_schema")
    prefix_digest = _sha_or_null(reuse["shared_prefix_token_ids_sha256"], "logical_work_shared_prefix")
    prefix_count = _int(reuse["shared_prefix_token_count"], "logical_work_shared_prefix_count", minimum=0, maximum=MAX_OUTPUT_TOKENS)
    required_hit_rate = reuse["required_eligible_hit_rate"]
    if required_hit_rate is not None:
        if type(required_hit_rate) not in {int, float} or not math.isfinite(float(required_hit_rate)):
            raise InferenceLaneContractError("logical_work_required_eligible_hit_rate_invalid")
        if not 0.0 <= float(required_hit_rate) <= 1.0:
            raise InferenceLaneContractError("logical_work_required_eligible_hit_rate_out_of_range")
    if lane == "exact_request_reuse":
        if (
            not indexes
            or exact_key is None
            or prefix_digest is not None
            or prefix_count != 0
            or required_hit_rate is None
        ):
            raise InferenceLaneContractError("exact_reuse_contract_invalid")
    elif lane == "shared_prefix_reuse":
        if (
            not indexes
            or exact_key is not None
            or prefix_digest is None
            or prefix_count < 1
            or required_hit_rate is None
        ):
            raise InferenceLaneContractError("prefix_reuse_contract_invalid")
    elif lane == "fresh_decode":
        if (
            indexes
            or exact_key is not None
            or prefix_digest is not None
            or prefix_count != 0
            or required_hit_rate is not None
        ):
            raise InferenceLaneContractError("fresh_decode_reuse_contract_invalid")
    else:  # pragma: no cover - caller validates lane before this point.
        raise InferenceLaneContractError("unknown_inference_lane")
    stable = json.loads(_canonical_json(raw))
    return LogicalWork(
        value=stable,
        sha256=sha256_json(stable),
        lane=lane,
        request_digests=request_digests,
        eligible_indexes=indexes,
    )


def _parse_runtime(value: Any) -> Mapping[str, Any]:
    raw = _exact_keys(
        value,
        {
            "backend",
            "host_hardware_sha256",
            "core_sha256",
            "runtime_sha256",
            "resolved_engine_config_sha256",
            "engine_id",
            "engine_commit",
            "metal_runtime_sha256",
            "weights_sha256",
            "tokenizer_sha256",
            "precision_id",
        },
        "runtime",
    )
    _string(raw["backend"], "runtime_backend", maximum=128)
    _string(raw["engine_id"], "runtime_engine_id", maximum=128)
    _revision(raw["engine_commit"], "runtime_engine_commit")
    _string(raw["precision_id"], "runtime_precision_id", maximum=128)
    for field in (
        "host_hardware_sha256",
        "core_sha256",
        "runtime_sha256",
        "resolved_engine_config_sha256",
        "metal_runtime_sha256",
        "weights_sha256",
        "tokenizer_sha256",
    ):
        _sha(raw[field], f"runtime_{field}")
    return json.loads(_canonical_json(raw))


def _parse_cache_policy(value: Any, *, lane: str, arm: str) -> Mapping[str, Any]:
    raw = _exact_keys(
        value,
        {
            "mode",
            "cache_history_sha256",
            "response_cache_enabled",
            "prefix_cache_enabled",
            "fallback_policy_sha256",
            "proposer_config_sha256",
        },
        "cache_policy",
    )
    mode = _string(raw["mode"], "cache_policy_mode", maximum=128)
    _sha(raw["cache_history_sha256"], "cache_policy_history")
    response_enabled = _bool(raw["response_cache_enabled"], "cache_policy_response_enabled")
    prefix_enabled = _bool(raw["prefix_cache_enabled"], "cache_policy_prefix_enabled")
    _sha(raw["fallback_policy_sha256"], "cache_policy_fallback")
    proposer = _sha_or_null(raw["proposer_config_sha256"], "cache_policy_proposer")
    if arm == "baseline":
        if mode != "target_only_no_reuse" or response_enabled or prefix_enabled or proposer is not None:
            raise InferenceLaneContractError("baseline_cache_policy_must_be_target_only_no_reuse")
    elif lane == "exact_request_reuse":
        if mode != "exact_request_reuse" or not response_enabled or prefix_enabled or proposer is None:
            raise InferenceLaneContractError("exact_reuse_candidate_cache_policy_invalid")
    elif lane == "shared_prefix_reuse":
        if mode != "shared_prefix_reuse" or response_enabled or not prefix_enabled or proposer is None:
            raise InferenceLaneContractError("prefix_reuse_candidate_cache_policy_invalid")
    elif lane == "fresh_decode":
        if mode != "fresh_decode_candidate" or response_enabled or prefix_enabled or proposer is None:
            raise InferenceLaneContractError("fresh_decode_candidate_cache_policy_invalid")
    else:  # pragma: no cover - caller validates lane before this point.
        raise InferenceLaneContractError("unknown_inference_lane")
    return json.loads(_canonical_json(raw))


def _parse_authorization(value: Any) -> Mapping[str, bool]:
    raw = _exact_keys(
        value,
        {"artifact_verified", "customer_selectable", "publication_eligible", "production_ready", "billing_eligible"},
        "authorization",
    )
    return {field: _bool(raw[field], f"authorization_{field}") for field in sorted(raw)}


def _parse_template(value: Any, label: str) -> PurePosixPath:
    text = _string(value, label, maximum=512)
    prefix = "{trial_dir}/"
    if not text.startswith(prefix):
        raise InferenceLaneContractError(f"{label}_must_be_under_trial_dir")
    suffix = text.removeprefix(prefix)
    parsed = PurePosixPath(suffix)
    if not suffix or parsed.is_absolute() or ".." in parsed.parts or parsed.suffix != ".json":
        raise InferenceLaneContractError(f"{label}_unsafe")
    return parsed


def _parse_command(value: Any, outputs: Sequence[str]) -> tuple[tuple[str, ...], tuple[FilePin, ...], int]:
    raw = _exact_keys(value, {"argv", "timeout_secs", "pinned_files"}, "command")
    argv_raw = raw["argv"]
    if not isinstance(argv_raw, list) or not 1 <= len(argv_raw) <= MAX_COMMAND_ARGUMENTS:
        raise InferenceLaneContractError("command_argv_invalid")
    argv = tuple(_string(item, f"command_argv_{index}", maximum=8_192) for index, item in enumerate(argv_raw))
    for output in outputs:
        if output not in argv:
            raise InferenceLaneContractError("command_must_explicitly_receive_every_trial_output")
    timeout = _int(raw["timeout_secs"], "command_timeout", minimum=1, maximum=MAX_TIMEOUT_SECONDS)
    pin_rows = raw["pinned_files"]
    if not isinstance(pin_rows, list) or not 1 <= len(pin_rows) <= MAX_COMMAND_ARGUMENTS:
        raise InferenceLaneContractError("command_pinned_files_invalid")
    pins: list[FilePin] = []
    roles: set[str] = set()
    executable: FilePin | None = None
    for index, item in enumerate(pin_rows):
        pin = _exact_keys(item, {"role", "path", "sha256"}, f"command_pin_{index}")
        role = _string(pin["role"], f"command_pin_role_{index}", maximum=128)
        if role in roles:
            raise InferenceLaneContractError("command_pin_roles_must_be_unique")
        roles.add(role)
        file_pin = _pin_file(pin["path"], pin["sha256"], f"command_pin_{index}", executable=role == "command_executable")
        if role == "command_executable":
            executable = file_pin
        pins.append(file_pin)
    if executable is None or argv[0] != str(executable.path):
        raise InferenceLaneContractError("command_executable_must_be_pinned_argv_zero")
    return argv, tuple(pins), timeout


def load_arm_manifest(path: Path, expected_arm: str) -> ArmManifest:
    if expected_arm not in {"baseline", "candidate"}:
        raise InferenceLaneContractError("expected_arm_invalid")
    manifest_pin = _pin_file(str(path), _sha256_file(path, maximum=MAX_MANIFEST_BYTES), f"{expected_arm}_manifest")
    raw, _bytes = _read_json(manifest_pin.path, f"{expected_arm}_manifest", maximum=MAX_MANIFEST_BYTES)
    value = _exact_keys(
        raw,
        {"schema_version", "kind", "arm", "lane", "logical_work", "runtime", "cache_policy", "authorization", "command", "trial_outputs"},
        f"{expected_arm}_manifest",
    )
    if value["schema_version"] != SCHEMA_VERSION or value["kind"] != ARM_MANIFEST_KIND or value["arm"] != expected_arm:
        raise InferenceLaneContractError("arm_manifest_identity_invalid")
    lane = _string(value["lane"], "arm_manifest_lane", maximum=64)
    if lane not in LANES:
        raise InferenceLaneContractError("arm_manifest_lane_unknown")
    logical_work = _parse_logical_work(value["logical_work"], lane)
    runtime = _parse_runtime(value["runtime"])
    cache_policy = _parse_cache_policy(value["cache_policy"], lane=lane, arm=expected_arm)
    authorization = _parse_authorization(value["authorization"])
    outputs = _exact_keys(value["trial_outputs"], {"arm_result", "stages"}, "trial_outputs")
    result_template = _parse_template(outputs["arm_result"], "arm_result_template")
    stage_values = _exact_keys(outputs["stages"], set(STAGES), "stage_templates")
    stage_templates = {stage: _parse_template(stage_values[stage], f"stage_template_{stage}") for stage in STAGES}
    all_templates = [result_template, *stage_templates.values()]
    if len(set(all_templates)) != len(all_templates):
        raise InferenceLaneContractError("trial_output_templates_must_be_distinct")
    command, command_pins, timeout = _parse_command(
        value["command"],
        ["{trial_dir}/" + str(result_template), *("{trial_dir}/" + str(stage_templates[stage]) for stage in STAGES)],
    )
    return ArmManifest(
        name=expected_arm,
        manifest_pin=manifest_pin,
        logical_work=logical_work,
        runtime=runtime,
        runtime_sha256=sha256_json(runtime),
        cache_policy=cache_policy,
        cache_policy_sha256=sha256_json(cache_policy),
        authorization=authorization,
        command=command,
        command_pins=command_pins,
        timeout_secs=timeout,
        result_template=result_template,
        stage_templates=stage_templates,
    )


def repeatable_trial_orders(trials: int) -> tuple[tuple[str, str], ...]:
    if type(trials) is not int or not (
        MIN_TRIALS <= trials <= MAX_TRIALS and trials % len(ABBA_ORDERS) == 0
    ):
        raise InferenceLaneContractError("trials_must_be_a_positive_abba_block")
    return tuple(ABBA_ORDERS[index % len(ABBA_ORDERS)] for index in range(trials))


def _within(path: Path, parent: Path, label: str) -> Path:
    try:
        resolved_parent = parent.resolve(strict=True)
        resolved = path.resolve(strict=False)
        resolved.relative_to(resolved_parent)
    except (OSError, ValueError) as exc:
        raise InferenceLaneContractError(f"{label}_escaped_trial_directory") from exc
    return resolved


def _outputs(arm: ArmManifest, trial_dir: Path) -> tuple[Path, Mapping[str, Path]]:
    result = _within(trial_dir / arm.result_template, trial_dir, "arm_result")
    stages = {stage: _within(trial_dir / arm.stage_templates[stage], trial_dir, f"stage_{stage}") for stage in STAGES}
    return result, stages


def _expand_argument(value: str, trial_dir: Path) -> str:
    return value.replace("{trial_dir}", str(trial_dir))


def _validate_stage_receipt(
    value: Mapping[str, Any],
    *,
    arm: ArmManifest,
    stage: str,
) -> None:
    raw = _exact_keys(
        value,
        {
            "schema_version",
            "kind",
            "stage",
            "arm",
            "lane",
            "logical_work_sha256",
            "runtime_identity_sha256",
            "cache_policy_sha256",
            "completed",
            "included_in_end_to_end_wall",
            "elapsed_ns",
            "artifact_sha256",
        },
        f"{stage}_stage_receipt",
    )
    if (
        raw["schema_version"] != SCHEMA_VERSION
        or raw["kind"] != STAGE_RECEIPT_KIND
        or raw["stage"] != stage
        or raw["arm"] != arm.name
        or raw["lane"] != arm.logical_work.lane
        or raw["logical_work_sha256"] != arm.logical_work.sha256
        or raw["runtime_identity_sha256"] != arm.runtime_sha256
        or raw["cache_policy_sha256"] != arm.cache_policy_sha256
        or _bool(raw["completed"], f"{stage}_stage_completed") is not True
        or _bool(raw["included_in_end_to_end_wall"], f"{stage}_stage_included") is not True
    ):
        raise InferenceLaneContractError(f"{stage}_stage_receipt_contract_invalid")
    _int(raw["elapsed_ns"], f"{stage}_stage_elapsed", minimum=0)
    _sha(raw["artifact_sha256"], f"{stage}_stage_artifact")


def _validate_outputs(value: Any, arm: ArmManifest) -> tuple[tuple[int, ...], ...]:
    if not isinstance(value, list) or len(value) != len(arm.logical_work.request_digests):
        raise InferenceLaneContractError("result_outputs_count_invalid")
    outputs: list[tuple[int, ...]] = []
    for index, row in enumerate(value):
        raw = _exact_keys(
            row,
            {"request_index", "request_sha256", "completion_token_ids", "completion_token_ids_sha256", "completion_token_count"},
            f"result_output_{index}",
        )
        if raw["request_index"] != index or raw["request_sha256"] != arm.logical_work.request_digests[index]:
            raise InferenceLaneContractError("result_output_request_binding_mismatch")
        tokens_raw = raw["completion_token_ids"]
        if not isinstance(tokens_raw, list) or len(tokens_raw) > arm.logical_work.value["max_output_tokens"]:
            raise InferenceLaneContractError("result_output_tokens_invalid")
        tokens = tuple(_int(token, f"result_output_token_{index}", minimum=0, maximum=(1 << 32) - 1) for token in tokens_raw)
        if raw["completion_token_count"] != len(tokens) or raw["completion_token_ids_sha256"] != sha256_json(list(tokens)):
            raise InferenceLaneContractError("result_output_token_digest_mismatch")
        outputs.append(tokens)
    return tuple(outputs)


def _validate_reuse_outcomes(value: Any, arm: ArmManifest) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != len(arm.logical_work.request_digests):
        raise InferenceLaneContractError("reuse_outcomes_count_invalid")
    expected = set(arm.logical_work.eligible_indexes)
    hits = 0
    for index, row in enumerate(value):
        raw = _exact_keys(row, {"request_index", "eligible", "hit"}, f"reuse_outcome_{index}")
        if raw["request_index"] != index or _bool(raw["eligible"], f"reuse_outcome_eligible_{index}") != (index in expected):
            raise InferenceLaneContractError("reuse_outcome_eligibility_not_predeclared")
        hit = _bool(raw["hit"], f"reuse_outcome_hit_{index}")
        if arm.name == "baseline" or arm.logical_work.lane == "fresh_decode":
            if hit:
                raise InferenceLaneContractError("reuse_hit_forbidden_for_this_arm_or_lane")
        elif hit and index not in expected:
            raise InferenceLaneContractError("reuse_hit_without_predeclared_eligibility")
        hits += int(hit)
    return len(expected), hits


def _validate_fallback(value: Any, arm: ArmManifest) -> Mapping[str, Any]:
    raw = _exact_keys(
        value,
        {
            "direct_target_decode_available",
            "direct_target_decode_validated",
            "used",
            "reason_code",
        },
        "fallback",
    )
    if _bool(raw["direct_target_decode_available"], "fallback_direct_target_available") is not True:
        raise InferenceLaneContractError("direct_target_fallback_not_available")
    if _bool(raw["direct_target_decode_validated"], "fallback_direct_target_validated") is not True:
        raise InferenceLaneContractError("direct_target_fallback_not_validated")
    used = _bool(raw["used"], "fallback_used")
    reason = _string(raw["reason_code"], "fallback_reason", maximum=256)
    if (not used and reason != "none") or (used and reason == "none"):
        raise InferenceLaneContractError("fallback_reason_state_invalid")
    return {
        "direct_target_decode_available": True,
        "direct_target_decode_validated": True,
        "used": used,
        "reason_code": reason,
    }


def _load_arm_result(
    result_path: Path,
    stage_paths: Mapping[str, Path],
    *,
    arm: ArmManifest,
) -> dict[str, Any]:
    stage_hashes: dict[str, str] = {}
    stage_artifacts: dict[str, str] = {}
    stage_elapsed_ns: dict[str, int] = {}
    stage_values: dict[str, Mapping[str, Any]] = {}
    for stage in STAGES:
        value, raw = _read_json(stage_paths[stage], f"{stage}_stage_receipt")
        _validate_stage_receipt(value, arm=arm, stage=stage)
        stage_hashes[stage] = _sha256_bytes(raw)
        stage_artifacts[stage] = str(value["artifact_sha256"])
        stage_elapsed_ns[stage] = int(value["elapsed_ns"])
        stage_values[stage] = value
    value, raw = _read_json(result_path, "arm_result")
    result = _exact_keys(
        value,
        {
            "schema_version",
            "kind",
            "arm",
            "lane",
            "logical_work_sha256",
            "runtime_identity_sha256",
            "cache_policy_sha256",
            "outputs",
            "reuse_outcomes",
            "stage_receipts",
            "fallback",
            "delivery_output_sha256",
        },
        "arm_result",
    )
    if (
        result["schema_version"] != SCHEMA_VERSION
        or result["kind"] != ARM_RESULT_KIND
        or result["arm"] != arm.name
        or result["lane"] != arm.logical_work.lane
        or result["logical_work_sha256"] != arm.logical_work.sha256
        or result["runtime_identity_sha256"] != arm.runtime_sha256
        or result["cache_policy_sha256"] != arm.cache_policy_sha256
    ):
        raise InferenceLaneContractError("arm_result_identity_binding_invalid")
    links = _exact_keys(result["stage_receipts"], set(STAGES), "arm_result_stage_receipts")
    if any(links[stage] != stage_hashes[stage] for stage in STAGES):
        raise InferenceLaneContractError("arm_result_stage_receipt_crosslink_mismatch")
    outputs = _validate_outputs(result["outputs"], arm)
    expected_delivery = sha256_json([sha256_json(list(tokens)) for tokens in outputs])
    if result["delivery_output_sha256"] != expected_delivery or stage_artifacts["delivery"] != expected_delivery:
        raise InferenceLaneContractError("delivery_output_digest_mismatch")
    eligible, hits = _validate_reuse_outcomes(result["reuse_outcomes"], arm)
    fallback = _validate_fallback(result["fallback"], arm)
    return {
        "result_sha256": _sha256_bytes(raw),
        "stage_receipts": stage_hashes,
        "stage_elapsed_ns": stage_elapsed_ns,
        "stage_values": stage_values,
        "arm_result": value,
        "outputs": outputs,
        "output_digests": tuple(sha256_json(list(tokens)) for tokens in outputs),
        "delivery_output_sha256": expected_delivery,
        "eligible_requests": eligible,
        "reuse_hits": hits,
        "fallback": fallback,
    }


def _run_arm(
    arm: ArmManifest,
    *,
    trial_dir: Path,
    clock_ns: Clock,
    runner: Runner,
) -> dict[str, Any]:
    trial_dir.mkdir(mode=0o700, parents=True)
    if trial_dir.is_symlink() or not trial_dir.is_dir():
        raise InferenceLaneContractError("trial_directory_unsafe")
    try:
        trial_dir = trial_dir.resolve(strict=True)
    except OSError as exc:
        raise InferenceLaneContractError("trial_directory_unresolvable") from exc
    result_path, stage_paths = _outputs(arm, trial_dir)
    destinations = (result_path, *(stage_paths[stage] for stage in STAGES))
    if any(path.exists() or path.is_symlink() for path in destinations):
        raise InferenceLaneContractError("trial_outputs_must_be_fresh")
    argv = tuple(_expand_argument(item, trial_dir) for item in arm.command)
    if any(str(path) not in argv for path in destinations):
        raise InferenceLaneContractError("expanded_command_lost_trial_output")
    for index, pin in enumerate(arm.command_pins):
        _revalidate_pin(pin, f"{arm.name}_command_{index}")
    _revalidate_pin(arm.manifest_pin, f"{arm.name}_manifest")
    started_ns = clock_ns()
    try:
        completed = runner(
            argv,
            cwd=str(trial_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=arm.timeout_secs,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise InferenceLaneContractError("arm_command_timeout") from exc
    if completed.returncode != 0:
        raise InferenceLaneContractError("arm_command_failed")
    # Parsing and validating every charged stage is inside the wall-clock
    # boundary, so an arm cannot defer delivery or verification post-timing.
    result = _load_arm_result(result_path, stage_paths, arm=arm)
    for index, pin in enumerate(arm.command_pins):
        _revalidate_pin(pin, f"{arm.name}_command_{index}")
    _revalidate_pin(arm.manifest_pin, f"{arm.name}_manifest")
    ended_ns = clock_ns()
    if type(started_ns) is not int or type(ended_ns) is not int or ended_ns <= started_ns:
        raise InferenceLaneContractError("clock_invalid")
    row = {
        "end_to_end_wall_ns": ended_ns - started_ns,
        "end_to_end_wall_ms": round((ended_ns - started_ns) / 1_000_000, 6),
        "command_argv_sha256": sha256_json(list(argv)),
        **result,
    }
    metadata_for = getattr(runner, "metadata_for", None)
    if callable(metadata_for):
        try:
            execution = metadata_for(argv)
        except Exception as exc:  # noqa: BLE001 - transport metadata is a receipt gate.
            raise InferenceLaneContractError("resident_execution_metadata_unavailable") from exc
        if not isinstance(execution, Mapping):
            raise InferenceLaneContractError("resident_execution_metadata_invalid")
        row["execution"] = dict(execution)
    return row


def _assert_stable(rows: Sequence[Mapping[str, Any]], arm: str) -> None:
    if not rows:
        raise InferenceLaneContractError("no_trial_rows")
    reference = rows[0]["outputs"]
    if any(row["outputs"] != reference for row in rows[1:]):
        raise InferenceLaneContractError(f"{arm}_outputs_not_repeat_stable")


def _nearest_rank(values: Sequence[int], quantile: float) -> int:
    if not values or not 0.0 < quantile <= 1.0:
        raise InferenceLaneContractError("percentile_arguments_invalid")
    ranked = sorted(values)
    return ranked[math.ceil(quantile * len(ranked)) - 1]


def _summary(values: Sequence[int]) -> Mapping[str, float | int]:
    if not values:
        raise InferenceLaneContractError("missing_wall_times")
    return {
        "p50_end_to_end_wall_ns": float(statistics.median(values)),
        "p95_end_to_end_wall_ns": _nearest_rank(values, 0.95),
        "min_end_to_end_wall_ns": min(values),
        "max_end_to_end_wall_ns": max(values),
    }


def _write_new(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise InferenceLaneContractError("receipt_write_failed") from exc
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _same_qualifying_runtime(baseline: ArmManifest, candidate: ArmManifest) -> None:
    """Reject a source pair that cannot truthfully map to one strict runtime.

    The strict receipt stores one engine/model/Metal identity and distinct
    baseline/candidate *configuration* hashes.  Therefore a different binary,
    weights, tokenizer, or Metal runtime belongs in a separate experiment, not
    in an adapted same-work result.
    """

    shared_fields = (
        "backend",
        "host_hardware_sha256",
        "core_sha256",
        "runtime_sha256",
        "engine_id",
        "engine_commit",
        "metal_runtime_sha256",
        "weights_sha256",
        "tokenizer_sha256",
        "precision_id",
    )
    for field in shared_fields:
        if baseline.runtime[field] != candidate.runtime[field]:
            raise InferenceLaneContractError(f"qualifying_receipt_runtime_mismatch_{field}")
    if baseline.authorization != candidate.authorization:
        raise InferenceLaneContractError("qualifying_receipt_authorization_mismatch")


def _strict_stage_times_ms(row: Mapping[str, Any]) -> dict[str, float]:
    """Account every integrated-wall nanosecond exactly once.

    Individual stage receipts describe work performed by the arm.  The command
    boundary also includes pinned-command dispatch and the receipt validation
    work needed before a customer-visible result can be returned.  That
    residual is explicitly charged to ``admission`` rather than silently
    omitted or used to manufacture a substage multiplier.
    """

    wall_ns = _int(row["end_to_end_wall_ns"], "qualifying_end_to_end_wall_ns", minimum=1)
    raw = row["stage_elapsed_ns"]
    if not isinstance(raw, Mapping) or set(raw) != set(STAGES):
        raise InferenceLaneContractError("qualifying_stage_set_mismatch")
    accounted_ns = {
        stage: _int(raw[stage], f"qualifying_{stage}_elapsed_ns", minimum=0)
        for stage in STAGES
    }
    measured_total_ns = sum(accounted_ns.values())
    if measured_total_ns > wall_ns:
        raise InferenceLaneContractError("qualifying_stage_elapsed_exceeds_integrated_wall")
    accounted_ns["admission"] += wall_ns - measured_total_ns
    return {stage: accounted_ns[stage] / 1_000_000 for stage in STAGES}


def _token_output_digest(outputs: Sequence[Sequence[int]]) -> tuple[str, int]:
    stable = [list(tokens) for tokens in outputs]
    token_count = sum(len(tokens) for tokens in stable)
    if token_count < 1:
        raise InferenceLaneContractError("qualifying_receipt_requires_nonempty_completion_tokens")
    return strict_receipt.sha256_json(stable), token_count


def _write_qualifying_artifact(
    work_root: Path,
    relative_path: str,
    value: Mapping[str, Any],
) -> tuple[str, str]:
    """Write one immutable strict-receipt artifact beneath the new work root."""

    path = _within(work_root / relative_path, work_root, "qualifying_artifact")
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.is_symlink() or not parent.is_dir():
        raise InferenceLaneContractError("qualifying_artifact_parent_unsafe")
    payload = _canonical_json(value) + b"\n"
    _write_new(path, payload)
    return relative_path, _sha256_bytes(payload)


def build_qualifying_receipt(
    *,
    work_root: Path,
    raw_measurement_receipt: Mapping[str, Any],
    raw_measurement_bytes: bytes,
    baseline: ArmManifest,
    candidate: ArmManifest,
    execution_rows: Sequence[Mapping[str, Any]],
    target_multiplier: float = DEFAULT_TARGET_MULTIPLIER,
) -> dict[str, Any]:
    """Adapt a completed eight-per-arm ABBA run into the strict receipt format.

    This is intentionally an adapter, not a second benchmark.  It retains a
    canonical capture for every actual arm execution, plus parity and quality
    artifacts that embed the raw ABBA measurement receipt and its immutable
    manifest/runtime/cache bindings.  A raw ABBA run that lacks any fact the
    strict contract needs is rejected instead of being padded with defaults.
    """

    if tuple(STAGES) != tuple(strict_receipt.STAGES):
        raise InferenceLaneContractError("strict_receipt_charged_stage_contract_mismatch")
    target_multiplier = _target_multiplier(target_multiplier)
    if len(execution_rows) != 2 * MIN_TRIALS:
        raise InferenceLaneContractError("qualifying_receipt_requires_eight_trials_per_arm")
    _same_qualifying_runtime(baseline, candidate)
    lane = baseline.logical_work.lane
    if candidate.logical_work.lane != lane:
        raise InferenceLaneContractError("qualifying_receipt_lane_mismatch")

    expected_arms = strict_receipt.ABBA * (len(execution_rows) // len(strict_receipt.ABBA))
    actual_arms = tuple(row.get("arm") for row in execution_rows)
    if actual_arms != expected_arms:
        raise InferenceLaneContractError("qualifying_receipt_execution_order_is_not_ABBA")

    logical = baseline.logical_work.value
    model = logical["model"]
    sampling = logical["sampling"]
    workload: dict[str, Any] = {
        "contract": "cx-openai-greedy-completions-v1",
        "request_set_sha256": strict_receipt.sha256_json(
            sorted(baseline.logical_work.request_digests)
        ),
        "request_order_sha256": logical["request_order_sha256"],
        "request_count": len(baseline.logical_work.request_digests),
        "max_output_tokens": logical["max_output_tokens"],
        "sampling": sampling,
        "concurrency": logical["concurrency"],
        "customer_visible_stages": list(STAGES),
    }
    workload_sha256 = strict_receipt.sha256_json(workload)
    source_receipt_sha256 = _sha256_bytes(raw_measurement_bytes)
    source_binding = {
        "measurement_receipt_sha256": source_receipt_sha256,
        "baseline_manifest": {
            "path": str(baseline.manifest_pin.path),
            "sha256": baseline.manifest_pin.sha256,
        },
        "candidate_manifest": {
            "path": str(candidate.manifest_pin.path),
            "sha256": candidate.manifest_pin.sha256,
        },
        # Every command pin is retained inside the strict-receipt artifacts.
        # Exact-cache manifests include the immutable endpoint-attestation pin,
        # so its digest cannot disappear when raw ABBA evidence is adapted.
        "baseline_command_pins": [
            {"path": str(pin.path), "sha256": pin.sha256}
            for pin in baseline.command_pins
        ],
        "candidate_command_pins": [
            {"path": str(pin.path), "sha256": pin.sha256}
            for pin in candidate.command_pins
        ],
        "baseline_runtime": dict(baseline.runtime),
        "candidate_runtime": dict(candidate.runtime),
        "baseline_cache_policy": dict(baseline.cache_policy),
        "candidate_cache_policy": dict(candidate.cache_policy),
        "logical_work": dict(logical),
        "logical_work_sha256": baseline.logical_work.sha256,
    }
    execution_context = raw_measurement_receipt.get("execution")
    if execution_context is not None:
        if not isinstance(execution_context, Mapping):
            raise InferenceLaneContractError("qualifying_receipt_execution_context_invalid")
        source_binding["execution_context"] = dict(execution_context)

    artifacts: list[dict[str, str]] = []
    samples: list[dict[str, Any]] = []
    capture_hashes: list[str] = []
    output_digests: list[str] = []
    output_counts: list[int] = []
    candidate_hits = 0
    candidate_eligible = 0
    candidate_count = 0
    candidate_fallback_validated = True
    for order, execution in enumerate(execution_rows):
        arm = execution["arm"]
        if arm not in {"baseline", "candidate"}:
            raise InferenceLaneContractError("qualifying_receipt_execution_arm_invalid")
        row = execution["row"]
        if not isinstance(row, Mapping):
            raise InferenceLaneContractError("qualifying_receipt_execution_row_invalid")
        stages_ms = _strict_stage_times_ms(row)
        outputs = row["outputs"]
        if not isinstance(outputs, Sequence):
            raise InferenceLaneContractError("qualifying_receipt_outputs_invalid")
        output_sha256, output_count = _token_output_digest(outputs)
        output_digests.append(output_sha256)
        output_counts.append(output_count)
        fallback = row["fallback"]
        if not isinstance(fallback, Mapping):
            raise InferenceLaneContractError("qualifying_receipt_fallback_invalid")
        if arm == "candidate":
            candidate_count += 1
            candidate_hits += _int(row["reuse_hits"], "qualifying_candidate_reuse_hits", minimum=0)
            candidate_eligible += _int(
                row["eligible_requests"], "qualifying_candidate_eligible_requests", minimum=0
            )
            candidate_fallback_validated = candidate_fallback_validated and bool(
                fallback.get("direct_target_decode_available")
            ) and bool(fallback.get("direct_target_decode_validated"))
        capture = {
            "schema_version": SCHEMA_VERSION,
            "record_kind": "cx_inference_abba_sample_capture_v1",
            "source_binding": source_binding,
            "order": order,
            "trial_index": execution["trial_index"],
            "arm": arm,
            "lane": lane,
            "end_to_end_wall_ns": row["end_to_end_wall_ns"],
            "end_to_end_wall_ms": row["end_to_end_wall_ms"],
            "stage_elapsed_ns": row["stage_elapsed_ns"],
            "stage_wall_accounted_ms": stages_ms,
            "command_argv_sha256": row["command_argv_sha256"],
            "execution": dict(row["execution"]) if isinstance(row.get("execution"), Mapping) else None,
            "arm_result_sha256": row["result_sha256"],
            "arm_result": row["arm_result"],
            "stage_receipt_sha256": row["stage_receipts"],
            "stage_receipts": row["stage_values"],
            "completion_token_ids": [list(tokens) for tokens in outputs],
            "completion_token_ids_sha256": output_sha256,
            "completion_token_count": output_count,
            "reuse": {
                "eligible_requests": row["eligible_requests"],
                "hits": row["reuse_hits"],
            },
            "fallback": fallback,
        }
        artifact_id = f"capture-{order:04d}"
        relative_path, capture_sha256 = _write_qualifying_artifact(
            work_root,
            f"qualifying-receipt-artifacts/captures/{order:04d}.json",
            capture,
        )
        artifacts.append(
            {
                "artifact_id": artifact_id,
                "kind": "sample_capture",
                "path": relative_path,
                "sha256": capture_sha256,
            }
        )
        capture_hashes.append(capture_sha256)
        samples.append(
            {
                "order": order,
                "sample_id": f"sample-{order:04d}-{arm}",
                "arm": arm,
                "workload_sha256": workload_sha256,
                "request_count": workload["request_count"],
                "elapsed_ms": row["end_to_end_wall_ns"] / 1_000_000,
                "stages_ms": stages_ms,
                "output_token_ids_sha256": output_sha256,
                "output_token_count": output_count,
                "quality_sha256": "",
                "capture_artifact_id": artifact_id,
                "status": "ok",
            }
        )
    if candidate_count != MIN_TRIALS:
        raise InferenceLaneContractError("qualifying_receipt_candidate_trial_count_invalid")
    if len(set(output_digests)) != 1 or len(set(output_counts)) != 1:
        raise InferenceLaneContractError("qualifying_receipt_outputs_not_exact_repeat_stable")
    if not candidate_fallback_validated:
        raise InferenceLaneContractError("qualifying_receipt_direct_target_fallback_not_validated")

    output_sha256 = output_digests[0]
    output_count = output_counts[0]
    parity = {
        "schema_version": SCHEMA_VERSION,
        "record_kind": "cx_inference_abba_parity_audit_v1",
        "policy": "exact_output_token_ids",
        "status": "passed",
        "baseline_candidate_exact": True,
        "output_token_ids_sha256": output_sha256,
        "output_token_count": output_count,
        "sample_capture_sha256": capture_hashes,
        "source_binding": source_binding,
        "measurement_receipt": raw_measurement_receipt,
    }
    parity_path, parity_sha256 = _write_qualifying_artifact(
        work_root, "qualifying-receipt-artifacts/parity.json", parity
    )
    artifacts.append(
        {
            "artifact_id": "parity",
            "kind": "parity_audit",
            "path": parity_path,
            "sha256": parity_sha256,
        }
    )
    quality = {
        "schema_version": SCHEMA_VERSION,
        "record_kind": "cx_inference_abba_quality_audit_v1",
        "policy": "exact_output_token_ids",
        "status": "passed",
        "summary": {
            "output_token_ids_sha256": output_sha256,
            "output_token_count": output_count,
            "sample_capture_sha256": capture_hashes,
            "source_measurement_receipt_sha256": source_receipt_sha256,
        },
    }
    quality_path, quality_sha256 = _write_qualifying_artifact(
        work_root, "qualifying-receipt-artifacts/quality.json", quality
    )
    artifacts.append(
        {
            "artifact_id": "quality",
            "kind": "quality_audit",
            "path": quality_path,
            "sha256": quality_sha256,
        }
    )
    for sample in samples:
        sample["quality_sha256"] = quality_sha256

    if lane == "fresh_decode":
        reuse: dict[str, Any] = {
            "scope": "none",
            "baseline_cache_state": "disabled",
            "candidate_cache_state": "disabled",
            "exact_response_reuse": False,
            "shared_prefix_tokens": 0,
            "coverage": None,
        }
    else:
        reuse_contract = logical["reuse_contract"]
        required_hit_rate = float(reuse_contract["required_eligible_hit_rate"])
        if candidate_eligible < 1 or candidate_hits > candidate_eligible:
            raise InferenceLaneContractError("qualifying_receipt_reuse_coverage_counts_invalid")
        observed_hit_rate = candidate_hits / candidate_eligible
        if observed_hit_rate < required_hit_rate:
            raise InferenceLaneContractError(
                "qualifying_receipt_reuse_coverage_below_declared_minimum"
            )
        reuse = {
            "scope": "exact_request" if lane == "exact_request_reuse" else "shared_prefix",
            "baseline_cache_state": "disabled",
            "candidate_cache_state": (
                "exact_response_cache_hit"
                if lane == "exact_request_reuse"
                else "prefix_kv_cache_hit"
            ),
            "exact_response_reuse": lane == "exact_request_reuse",
            "shared_prefix_tokens": (
                0 if lane == "exact_request_reuse" else reuse_contract["shared_prefix_token_count"]
            ),
            "coverage": {
                "coverage_workload_sha256": workload_sha256,
                "observed_requests": len(baseline.logical_work.request_digests) * candidate_count,
                "eligible_requests": candidate_eligible,
                "eligible_hits": candidate_hits,
                "eligible_request_fraction": candidate_eligible
                / (len(baseline.logical_work.request_digests) * candidate_count),
                "eligible_hit_rate": observed_hit_rate,
                "required_eligible_hit_rate": required_hit_rate,
            },
        }

    baseline_times = [float(sample["elapsed_ms"]) for sample in samples if sample["arm"] == "baseline"]
    candidate_times = [float(sample["elapsed_ms"]) for sample in samples if sample["arm"] == "candidate"]
    baseline_p50 = strict_receipt._nearest_rank(baseline_times, 50)
    baseline_p95 = strict_receipt._nearest_rank(baseline_times, 95)
    candidate_p50 = strict_receipt._nearest_rank(candidate_times, 50)
    candidate_p95 = strict_receipt._nearest_rank(candidate_times, 95)
    strict_lane = strict_receipt.LANES[lane]
    receipt_value: dict[str, Any] = {
        "schema_version": strict_receipt.SCHEMA_VERSION,
        "record_kind": strict_receipt.RECORD_KIND,
        "receipt_id": f"abba-{lane.replace('_', '-')}-{baseline.logical_work.sha256[:12]}",
        "claim_scope": "customer_visible_inference_request_turnaround",
        "lane": lane,
        "scorecard_binding": {
            "lane_id": strict_lane["scorecard_lane_id"],
            "display_name": f"ABBA {lane.replace('_', ' ')}",
            "comparison_group": f"abba-{lane.replace('_', '-')}",
        },
        "workload": workload,
        "workload_sha256": workload_sha256,
        "runtime": {
            "engine_id": baseline.runtime["engine_id"],
            "engine_commit": baseline.runtime["engine_commit"],
            "metal_runtime_sha256": baseline.runtime["metal_runtime_sha256"],
            "model_id": model["model_id"],
            "model_revision": model["model_revision"],
            "weights_sha256": baseline.runtime["weights_sha256"],
            "tokenizer_sha256": baseline.runtime["tokenizer_sha256"],
            "precision_id": baseline.runtime["precision_id"],
            "baseline_config_sha256": baseline.runtime["resolved_engine_config_sha256"],
            "candidate_config_sha256": candidate.runtime["resolved_engine_config_sha256"],
        },
        "comparison": {
            "claim_axis": "inference_request_turnaround",
            "timing_scope": "integrated_wall",
            "same_logical_work": True,
            "baseline_mode": "target_only",
            "candidate_mode": candidate.cache_policy["mode"],
            "target_multiplier": target_multiplier,
        },
        "reuse": reuse,
        "fallback": {
            "enabled": True,
            "mode": "target_only_direct_decode",
            "trigger": "parity_or_confidence_failure",
            "validated": True,
        },
        "authorization": dict(candidate.authorization),
        "attestation": {
            "evidence_class": "physical_local_unattested",
            "independent_attestation": False,
        },
        "artifacts": artifacts,
        "samples": samples,
        "parity": {
            "policy": "exact_output_token_ids",
            "status": "passed",
            "baseline_candidate_exact": True,
            "output_token_ids_sha256": output_sha256,
            "output_token_count": output_count,
            "parity_artifact_id": "parity",
        },
        "quality": {
            "policy": "exact_output_token_ids",
            "status": "passed",
            "summary_sha256": quality_sha256,
            "quality_artifact_id": "quality",
        },
        "statistics": {
            "unit": "ms/request",
            "baseline": {"p50_ms": baseline_p50, "p95_ms": baseline_p95},
            "candidate": {"p50_ms": candidate_p50, "p95_ms": candidate_p95},
            "p50_multiplier": baseline_p50 / candidate_p50,
            "p95_multiplier": baseline_p95 / candidate_p95,
        },
        "receipt_sha256": "",
    }
    receipt_value["receipt_sha256"] = strict_receipt.receipt_sha256(receipt_value)
    try:
        strict_receipt.validate_receipt(receipt_value)
        strict_receipt.verify_artifact_bindings(receipt_value, work_root)
    except strict_receipt.InferenceReceiptError as exc:
        raise InferenceLaneContractError(f"qualifying_receipt_contract_rejected_{exc}") from exc
    return receipt_value


def unmeasured_contract() -> dict[str, Any]:
    """Describe requirements without manufacturing any timing or speed claim."""

    return {
        "schema_version": SCHEMA_VERSION,
        "receipt_kind": KIND,
        "measurement_status": "unmeasured_no_real_paired_trials",
        "lanes": list(LANES),
        "claim": {
            "direct_end_to_end_p50_multiplier": None,
            "aggregate_multiplier": None,
            "eligible_for_public_50x_claim": False,
            "reason": "no real pinned baseline/candidate ABBA trials have completed",
        },
        "required_evidence": {
            "same_logical_work_per_lane": True,
            "pinned_model_tokenizer_runtime_cache_history": True,
            "exact_completion_token_ids": True,
            "charged_stages": list(STAGES),
            "abba_blocks_of_four": True,
            "minimum_trials_per_arm": MIN_TRIALS,
            "strict_receipt_adapter": "--qualifying-receipt-out",
            "predeclared_reuse_eligibility_and_observed_hit_coverage": True,
            "direct_target_fallback": True,
            "ratio_stacking_forbidden": True,
        },
    }


def benchmark(
    config: RunConfig,
    *,
    clock_ns: Clock = time.perf_counter_ns,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Run one isolated lane's complete ABBA matrix and write an immutable receipt."""

    config.validate()
    target_multiplier = _target_multiplier(config.target_multiplier)
    execution_context: Mapping[str, Any]
    if config.resident_session is not None:
        if runner is not None:
            raise InferenceLaneContractError("resident_session_cannot_use_external_runner")
        try:
            import cx_inference_resident_worker_v1 as resident_worker

            runner = resident_worker.ResidentRpcRunner(config.resident_session)
            execution_context = runner.context()
        except Exception as exc:  # noqa: BLE001 - fail closed on the transport boundary.
            raise InferenceLaneContractError("resident_session_invalid_or_unavailable") from exc
    else:
        runner = subprocess.run if runner is None else runner
        execution_context = {
            "execution_transport": "pinned_subprocess_command",
            "lifecycle": {
                "mode": "per_arm_process",
                "worker_startup_excluded": False,
                "cold_start_measured": True,
            },
        }
    baseline = load_arm_manifest(config.baseline_manifest, "baseline")
    candidate = load_arm_manifest(config.candidate_manifest, "candidate")
    if baseline.logical_work.lane != candidate.logical_work.lane:
        raise InferenceLaneContractError("baseline_candidate_lane_mismatch")
    if _canonical_json(baseline.logical_work.value) != _canonical_json(candidate.logical_work.value):
        raise InferenceLaneContractError("baseline_candidate_logical_work_mismatch")
    if baseline.logical_work.sha256 != candidate.logical_work.sha256:
        raise InferenceLaneContractError("baseline_candidate_logical_work_digest_mismatch")
    config.work_root.mkdir(mode=0o700)
    if config.work_root.is_symlink() or not config.work_root.is_dir():
        raise InferenceLaneContractError("work_root_create_failed")
    arms = {"baseline": baseline, "candidate": candidate}
    trials: list[dict[str, Any]] = []
    baseline_rows: list[Mapping[str, Any]] = []
    candidate_rows: list[Mapping[str, Any]] = []
    execution_rows: list[dict[str, Any]] = []
    for index, order in enumerate(repeatable_trial_orders(config.trials)):
        trial_root = config.work_root / f"trial-{index:04d}"
        trial_root.mkdir(mode=0o700)
        arm_rows: dict[str, Any] = {}
        for arm_name in order:
            arm_rows[arm_name] = _run_arm(arms[arm_name], trial_dir=trial_root / arm_name, clock_ns=clock_ns, runner=runner)
            execution_rows.append(
                {
                    "trial_index": index,
                    "arm": arm_name,
                    "row": arm_rows[arm_name],
                }
            )
        if arm_rows["baseline"]["outputs"] != arm_rows["candidate"]["outputs"]:
            raise InferenceLaneContractError("baseline_candidate_exact_token_parity_failed")
        baseline_rows.append(arm_rows["baseline"])
        candidate_rows.append(arm_rows["candidate"])
        trials.append(
            {
                "trial_index": index,
                "arm_order": list(order),
                "arms": {
                    name: {
                        "end_to_end_wall_ns": arm_rows[name]["end_to_end_wall_ns"],
                        "end_to_end_wall_ms": arm_rows[name]["end_to_end_wall_ms"],
                        "command_argv_sha256": arm_rows[name]["command_argv_sha256"],
                        "execution": arm_rows[name].get("execution"),
                        "result_sha256": arm_rows[name]["result_sha256"],
                        "stage_receipts": arm_rows[name]["stage_receipts"],
                        "delivery_output_sha256": arm_rows[name]["delivery_output_sha256"],
                        "reuse": {
                            "eligible_requests": arm_rows[name]["eligible_requests"],
                            "hits": arm_rows[name]["reuse_hits"],
                        },
                        "fallback": arm_rows[name]["fallback"],
                    }
                    for name in ("baseline", "candidate")
                },
            }
        )
    _assert_stable(baseline_rows, "baseline")
    _assert_stable(candidate_rows, "candidate")
    baseline_summary = _summary([int(row["end_to_end_wall_ns"]) for row in baseline_rows])
    candidate_summary = _summary([int(row["end_to_end_wall_ns"]) for row in candidate_rows])
    p50_multiplier = float(baseline_summary["p50_end_to_end_wall_ns"]) / float(candidate_summary["p50_end_to_end_wall_ns"])
    p95_multiplier = float(baseline_summary["p95_end_to_end_wall_ns"]) / float(candidate_summary["p95_end_to_end_wall_ns"])
    eligible = len(candidate.logical_work.eligible_indexes)
    total_opportunities = eligible * len(candidate_rows)
    total_hits = sum(int(row["reuse_hits"]) for row in candidate_rows)
    observed_hit_rate = None if total_opportunities == 0 else round(total_hits / total_opportunities, 9)
    authorizations_ready = all(candidate.authorization.values())
    p50_target_met = p50_multiplier >= target_multiplier
    p95_target_met = p95_multiplier >= target_multiplier
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "receipt_kind": KIND,
        "measurement_status": "real_paired_trials_recorded_local_unattested",
        "lane": baseline.logical_work.lane,
        "claim": {
            "direct_end_to_end_p50_multiplier": round(p50_multiplier, 9),
            "direct_end_to_end_p95_multiplier": round(p95_multiplier, 9),
            "aggregate_multiplier": None,
            "declared_target_multiplier": target_multiplier,
            "eligible_for_public_50x_claim": False,
            "reason": "raw local receipt requires independent evidence/scorecard promotion; no stage or lane ratios were combined",
        },
        "identity": {
            "logical_work": dict(baseline.logical_work.value),
            "logical_work_sha256": baseline.logical_work.sha256,
            "baseline_manifest": {"path": str(baseline.manifest_pin.path), "sha256": baseline.manifest_pin.sha256},
            "candidate_manifest": {"path": str(candidate.manifest_pin.path), "sha256": candidate.manifest_pin.sha256},
            "baseline_runtime_identity_sha256": baseline.runtime_sha256,
            "candidate_runtime_identity_sha256": candidate.runtime_sha256,
            "baseline_cache_policy_sha256": baseline.cache_policy_sha256,
            "candidate_cache_policy_sha256": candidate.cache_policy_sha256,
        },
        "execution": dict(execution_context),
        "timing_contract": {
            "one_boundary_per_arm": True,
            "starts_before": (
                "resident RPC request serialization"
                if config.resident_session is not None
                else "pinned command invocation"
            ),
            "ends_after": "all charged stage receipts, result receipt, exact-token validation, and pin revalidation",
            "charged_stages": list(STAGES),
            "substage_elapsed_ns_used_for_ratio": False,
            "ratio_rule": "only p50/p95 of complete baseline and candidate arm walls in this single lane",
        },
        "trial_plan": {"algorithm": "ABBA", "trials": config.trials, "orders": [list(order) for order in repeatable_trial_orders(config.trials)]},
        "trials": trials,
        "summary": {"baseline": baseline_summary, "candidate": candidate_summary},
        "reuse_coverage": {
            "lane": baseline.logical_work.lane,
            "eligible_request_indexes": list(candidate.logical_work.eligible_indexes),
            "eligible_request_opportunities": total_opportunities,
            "observed_hits": total_hits,
            "observed_eligible_hit_rate": observed_hit_rate,
            "not_applicable_for_fresh_decode": baseline.logical_work.lane == "fresh_decode",
        },
        "promotion_gate": {
            "same_logical_work": True,
            "exact_token_parity_each_pair": True,
            "baseline_repeat_stable": True,
            "candidate_repeat_stable": True,
            "abba_complete": True,
            "all_stages_charged": True,
            "runtime_and_cache_history_bound": True,
            "direct_target_fallback_available": all(bool(row["fallback"]["direct_target_decode_available"]) for row in candidate_rows),
            "direct_target_fallback_validated": all(bool(row["fallback"]["direct_target_decode_validated"]) for row in candidate_rows),
            "reuse_coverage_observed": observed_hit_rate is not None or baseline.logical_work.lane == "fresh_decode",
            "candidate_authorization_flags_all_true": authorizations_ready,
            "declared_target_multiplier": target_multiplier,
            "p50_reaches_declared_target": p50_target_met,
            "p95_reaches_declared_target": p95_target_met,
            "p50_reaches_50x": p50_multiplier >= DEFAULT_TARGET_MULTIPLIER,
            "p95_reaches_50x": p95_multiplier >= DEFAULT_TARGET_MULTIPLIER,
            "external_scorecard_promotion_required": True,
        },
    }
    raw_measurement_bytes = _canonical_json(receipt) + b"\n"
    _write_new(config.receipt_out, raw_measurement_bytes)
    if config.qualifying_receipt_out is not None:
        qualifying_receipt = build_qualifying_receipt(
            work_root=config.work_root,
            raw_measurement_receipt=receipt,
            raw_measurement_bytes=raw_measurement_bytes,
            baseline=baseline,
            candidate=candidate,
            execution_rows=execution_rows,
            target_multiplier=target_multiplier,
        )
        _write_new(
            config.qualifying_receipt_out,
            strict_receipt.canonical_json_bytes(qualifying_receipt) + b"\n",
        )
    return receipt


def _positive_int(raw: str) -> int:
    try:
        value = int(raw, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def _positive_multiplier(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a finite number") from exc
    if not math.isfinite(value) or value < 1.0:
        raise argparse.ArgumentTypeError("must be finite and at least 1")
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="emit the no-run contract")
    parser.add_argument("--baseline-manifest", type=Path)
    parser.add_argument("--candidate-manifest", type=Path)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--receipt-out", type=Path)
    parser.add_argument(
        "--resident-session",
        type=Path,
        help=(
            "immutable cx_inference_resident_exact_cache_session_v1 descriptor; "
            "uses one pre-started local worker and charges resident RPC per arm"
        ),
    )
    parser.add_argument(
        "--qualifying-receipt-out",
        type=Path,
        help=(
            "optional strict cx_inference_50x_lane_receipt output; requires eight "
            "ABBA trial pairs and writes retained artifacts beneath --work-root"
        ),
    )
    parser.add_argument("--trials", type=_positive_int)
    parser.add_argument(
        "--target-multiplier",
        type=_positive_multiplier,
        default=DEFAULT_TARGET_MULTIPLIER,
        help=(
            "declared target for the strict lane receipt; defaults to 50 and is "
            "not a license to relabel one lane as another"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    inputs = ("baseline_manifest", "candidate_manifest", "work_root", "receipt_out", "trials")
    if args.status:
        if any(getattr(args, name) is not None for name in (*inputs, "resident_session")):
            print("cx inference lane contract rejected: --status cannot be combined with run inputs", file=sys.stderr)
            return 2
        sys.stdout.buffer.write(_canonical_json(unmeasured_contract()) + b"\n")
        return 0
    if any(getattr(args, name) is None for name in inputs):
        print("cx inference lane contract rejected: explicit manifests, work root, receipt, and ABBA trials are required", file=sys.stderr)
        return 2
    try:
        receipt = benchmark(
            RunConfig(
                baseline_manifest=args.baseline_manifest,
                candidate_manifest=args.candidate_manifest,
                work_root=args.work_root,
                receipt_out=args.receipt_out,
                trials=args.trials,
                qualifying_receipt_out=args.qualifying_receipt_out,
                resident_session=args.resident_session,
                target_multiplier=args.target_multiplier,
            )
        )
    except Exception as exc:  # noqa: BLE001 - fail closed at this CLI boundary.
        print(f"cx inference lane contract rejected: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(_canonical_json(receipt) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
