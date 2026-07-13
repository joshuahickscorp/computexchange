#!/usr/bin/env python3
"""Validate comparable benchmark manifests and evaluate per-lane proof records.

This runner ingests measurements; it does not manufacture or provision physical
benchmarks. Synthetic fixtures exercise only the contract and evaluator paths.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parent.parent
PERF_ROOT = ROOT / "proof" / "performance"
FIXTURE_ROOT = PERF_ROOT / "fixtures"
DEFAULT_SCHEMA = PERF_ROOT / "benchmark-manifest.schema.json"
DEFAULT_PROOF = PERF_ROOT / "synthetic-evaluator-proof.generated.json"
DEFAULT_BASELINE_MANIFEST = FIXTURE_ROOT / "synthetic-baseline.manifest.json"
DEFAULT_CANDIDATE_MANIFEST = FIXTURE_ROOT / "synthetic-candidate.manifest.json"
DEFAULT_BASELINE_OBSERVATIONS = FIXTURE_ROOT / "synthetic-baseline.observations.json"
DEFAULT_PASS_OBSERVATIONS = FIXTURE_ROOT / "synthetic-candidate-pass.observations.json"
DEFAULT_FAULT_OBSERVATIONS = FIXTURE_ROOT / "synthetic-candidate-fault.observations.json"

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_DIFFERENCES = {
    "lane_id",
    "runtime.engine",
    "runtime.agent",
    "runtime.toolchain",
    "hardware.host_id_sha256",
    "hardware.sku",
    "hardware.accelerator_count",
    "hardware.accelerator_memory_gb",
    "hardware.system_memory_gb",
    "hardware.cpu",
    "hardware.os",
    "hardware.os_version",
    "hardware.driver",
    "hardware.firmware",
    "environment.power.source",
    "environment.power.mode",
    "environment.power.limit_watts",
    "environment.power.measurement_source",
    "environment.thermal.temperature_c",
    "environment.thermal.sensor",
    "environment.thermal.fan_policy",
    "environment.competing_load.process_snapshot_sha256",
    "environment.competing_load.cpu_utilization_pct",
    "environment.competing_load.gpu_utilization_pct",
    "environment.competing_load.memory_used_gb",
}
EVENT_TYPES = {"oom", "restart", "disconnect"}
PLACEHOLDER_VALUES = {"unknown", "latest", "unversioned", "tbd", "todo", "replace-me"}


class ContractError(ValueError):
    """A manifest or observation violates the proof contract."""


class IncomparableManifestError(ContractError):
    """Two individually valid manifests differ outside declared experiment axes."""


def _duplicate_safe_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path | str) -> dict[str, Any]:
    source = Path(path)
    try:
        with source.open("r", encoding="utf-8") as handle:
            value = json.load(handle, object_pairs_hook=_duplicate_safe_object)
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{source}: root must be an object")
    return value


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def pretty_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{context} must be an object")
    return value


def _exact(value: Mapping[str, Any], fields: set[str], context: str) -> None:
    present = set(value)
    missing = sorted(fields - present)
    unknown = sorted(present - fields)
    if missing:
        raise ContractError(f"{context} missing field(s): {', '.join(missing)}")
    if unknown:
        raise ContractError(f"{context} has unknown field(s): {', '.join(unknown)}")


def _string(value: Any, context: str, *, pinned: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{context} must be a non-empty string")
    if pinned and value.strip().lower() in PLACEHOLDER_VALUES:
        raise ContractError(f"{context} cannot be the placeholder {value!r}")
    return value


def _identifier(value: Any, context: str) -> str:
    result = _string(value, context)
    if not ID_RE.fullmatch(result):
        raise ContractError(f"{context} has invalid identifier {result!r}")
    return result


def _sha256(value: Any, context: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ContractError(f"{context} must be a lowercase 64-character SHA-256")
    return value


def _number(
    value: Any,
    context: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    nullable: bool = False,
) -> float | int | None:
    if nullable and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ContractError(f"{context} must be a finite number" + (" or null" if nullable else ""))
    if minimum is not None and value < minimum:
        raise ContractError(f"{context} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ContractError(f"{context} must be <= {maximum}")
    return value


def _integer(value: Any, context: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractError(f"{context} must be an integer >= {minimum}")
    return value


def _boolean(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{context} must be boolean")
    return value


def _enum(value: Any, choices: set[str], context: str) -> str:
    result = _string(value, context)
    if result not in choices:
        raise ContractError(f"{context} must be one of: {', '.join(sorted(choices))}")
    return result


def _versioned_build(value: Any, context: str) -> None:
    row = _object(value, context)
    _exact(row, {"name", "version", "build_sha256"}, context)
    _string(row["name"], f"{context}.name", pinned=True)
    _string(row["version"], f"{context}.version", pinned=True)
    _sha256(row["build_sha256"], f"{context}.build_sha256")


def validate_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one manifest and return a stable normalized copy."""

    manifest = _object(value, "manifest")
    _exact(
        manifest,
        {
            "schema_version",
            "manifest_id",
            "comparison_id",
            "arm",
            "lane_id",
            "evidence_class",
            "allowed_differences",
            "workload",
            "runtime",
            "hardware",
            "environment",
            "timing_scope",
            "sampling",
            "regression_policy",
        },
        "manifest",
    )
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ContractError("manifest.schema_version must be integer 1")
    _identifier(manifest["manifest_id"], "manifest.manifest_id")
    _identifier(manifest["comparison_id"], "manifest.comparison_id")
    _enum(manifest["arm"], {"baseline", "candidate"}, "manifest.arm")
    _identifier(manifest["lane_id"], "manifest.lane_id")
    evidence_class = _enum(
        manifest["evidence_class"], {"synthetic", "physical"}, "manifest.evidence_class"
    )

    differences = manifest["allowed_differences"]
    if not isinstance(differences, list) or not differences:
        raise ContractError("manifest.allowed_differences must be a non-empty list")
    for index, path in enumerate(differences):
        _enum(path, ALLOWED_DIFFERENCES, f"manifest.allowed_differences[{index}]")
    if len(differences) != len(set(differences)):
        raise ContractError("manifest.allowed_differences contains duplicates")

    workload = _object(manifest["workload"], "manifest.workload")
    _exact(workload, {"job_type", "model", "precision", "prompt_corpus", "output", "batch"}, "manifest.workload")
    _identifier(workload["job_type"], "manifest.workload.job_type")

    model = _object(workload["model"], "manifest.workload.model")
    _exact(model, {"id", "revision", "weights_sha256", "tokenizer_sha256"}, "manifest.workload.model")
    _identifier(model["id"], "manifest.workload.model.id")
    _string(model["revision"], "manifest.workload.model.revision", pinned=True)
    _sha256(model["weights_sha256"], "manifest.workload.model.weights_sha256")
    _sha256(model["tokenizer_sha256"], "manifest.workload.model.tokenizer_sha256")

    precision = _object(workload["precision"], "manifest.workload.precision")
    _exact(precision, {"compute_dtype", "weight_dtype", "accumulator_dtype", "quantization"}, "manifest.workload.precision")
    for field in ("compute_dtype", "weight_dtype", "accumulator_dtype"):
        _string(precision[field], f"manifest.workload.precision.{field}", pinned=True)
    quant = _object(precision["quantization"], "manifest.workload.precision.quantization")
    _exact(quant, {"scheme", "bits", "group_size", "calibration_sha256"}, "manifest.workload.precision.quantization")
    scheme = _string(quant["scheme"], "manifest.workload.precision.quantization.scheme", pinned=True)
    bits = _integer(quant["bits"], "manifest.workload.precision.quantization.bits")
    if quant["group_size"] is not None:
        _integer(quant["group_size"], "manifest.workload.precision.quantization.group_size", minimum=1)
    _sha256(quant["calibration_sha256"], "manifest.workload.precision.quantization.calibration_sha256", nullable=True)
    if scheme == "none" and (bits != 0 or quant["group_size"] is not None or quant["calibration_sha256"] is not None):
        raise ContractError("quantization scheme 'none' requires bits=0 and null group/calibration")
    if scheme != "none" and bits == 0:
        raise ContractError("a quantized scheme requires bits > 0")

    corpus = _object(workload["prompt_corpus"], "manifest.workload.prompt_corpus")
    _exact(corpus, {"id", "sha256", "record_count", "format"}, "manifest.workload.prompt_corpus")
    _identifier(corpus["id"], "manifest.workload.prompt_corpus.id")
    _sha256(corpus["sha256"], "manifest.workload.prompt_corpus.sha256")
    _integer(corpus["record_count"], "manifest.workload.prompt_corpus.record_count", minimum=1)
    _string(corpus["format"], "manifest.workload.prompt_corpus.format", pinned=True)

    output = _object(workload["output"], "manifest.workload.output")
    _exact(
        output,
        {
            "requested_tokens_per_prompt",
            "include_prompt_tokens",
            "stop_conditions_sha256",
            "throughput_unit",
        },
        "manifest.workload.output",
    )
    _integer(output["requested_tokens_per_prompt"], "manifest.workload.output.requested_tokens_per_prompt", minimum=1)
    include_prompt_tokens = _boolean(
        output["include_prompt_tokens"], "manifest.workload.output.include_prompt_tokens"
    )
    _enum(output["throughput_unit"], {"output_token"}, "manifest.workload.output.throughput_unit")
    if include_prompt_tokens:
        raise ContractError(
            "manifest.workload.output.include_prompt_tokens must be false until prompt-token counts are artifact-derived"
        )
    _sha256(output["stop_conditions_sha256"], "manifest.workload.output.stop_conditions_sha256")

    batch = _object(workload["batch"], "manifest.workload.batch")
    _exact(batch, {"mode", "batch_size", "max_batch_size", "max_wait_ms", "padding", "ordering"}, "manifest.workload.batch")
    mode = _enum(batch["mode"], {"fixed", "dynamic"}, "manifest.workload.batch.mode")
    batch_size = _integer(batch["batch_size"], "manifest.workload.batch.batch_size", minimum=1)
    max_batch = _integer(batch["max_batch_size"], "manifest.workload.batch.max_batch_size", minimum=1)
    _number(batch["max_wait_ms"], "manifest.workload.batch.max_wait_ms", minimum=0)
    _string(batch["padding"], "manifest.workload.batch.padding", pinned=True)
    _string(batch["ordering"], "manifest.workload.batch.ordering", pinned=True)
    if batch_size > max_batch:
        raise ContractError("batch_size cannot exceed max_batch_size")
    if mode == "fixed" and batch_size != max_batch:
        raise ContractError("fixed batch policy requires batch_size == max_batch_size")

    runtime = _object(manifest["runtime"], "manifest.runtime")
    _exact(runtime, {"engine", "agent", "toolchain"}, "manifest.runtime")
    _versioned_build(runtime["engine"], "manifest.runtime.engine")
    _versioned_build(runtime["agent"], "manifest.runtime.agent")
    toolchain = runtime["toolchain"]
    if not isinstance(toolchain, list) or not toolchain:
        raise ContractError("manifest.runtime.toolchain must be a non-empty list")
    tool_names: list[str] = []
    for index, component in enumerate(toolchain):
        context = f"manifest.runtime.toolchain[{index}]"
        _versioned_build(component, context)
        tool_names.append(component["name"])
    if len(tool_names) != len(set(tool_names)):
        raise ContractError("manifest.runtime.toolchain contains duplicate component names")

    hardware = _object(manifest["hardware"], "manifest.hardware")
    _exact(
        hardware,
        {"host_id_sha256", "sku", "accelerator_count", "accelerator_memory_gb", "system_memory_gb", "cpu", "os", "os_version", "driver", "firmware"},
        "manifest.hardware",
    )
    _sha256(hardware["host_id_sha256"], "manifest.hardware.host_id_sha256")
    for field in ("sku", "cpu", "os", "os_version", "driver", "firmware"):
        _string(hardware[field], f"manifest.hardware.{field}", pinned=True)
    _integer(hardware["accelerator_count"], "manifest.hardware.accelerator_count", minimum=1)
    _number(hardware["accelerator_memory_gb"], "manifest.hardware.accelerator_memory_gb", minimum=0)
    _number(hardware["system_memory_gb"], "manifest.hardware.system_memory_gb", minimum=0.001)

    environment = _object(manifest["environment"], "manifest.environment")
    _exact(environment, {"power", "thermal", "competing_load"}, "manifest.environment")
    power = _object(environment["power"], "manifest.environment.power")
    _exact(power, {"source", "mode", "limit_watts", "measurement_source"}, "manifest.environment.power")
    for field in ("source", "mode", "measurement_source"):
        _string(power[field], f"manifest.environment.power.{field}", pinned=True)
    _number(power["limit_watts"], "manifest.environment.power.limit_watts", minimum=0.001, nullable=True)
    thermal = _object(environment["thermal"], "manifest.environment.thermal")
    _exact(thermal, {"state", "temperature_c", "sensor", "fan_policy"}, "manifest.environment.thermal")
    for field in ("state", "sensor", "fan_policy"):
        _string(thermal[field], f"manifest.environment.thermal.{field}", pinned=True)
    _number(thermal["temperature_c"], "manifest.environment.thermal.temperature_c", minimum=-50, maximum=150, nullable=True)
    load = _object(environment["competing_load"], "manifest.environment.competing_load")
    _exact(load, {"policy", "process_snapshot_sha256", "cpu_utilization_pct", "gpu_utilization_pct", "memory_used_gb"}, "manifest.environment.competing_load")
    _enum(load["policy"], {"idle", "recorded"}, "manifest.environment.competing_load.policy")
    _sha256(load["process_snapshot_sha256"], "manifest.environment.competing_load.process_snapshot_sha256")
    _number(load["cpu_utilization_pct"], "manifest.environment.competing_load.cpu_utilization_pct", minimum=0, maximum=100)
    _number(load["gpu_utilization_pct"], "manifest.environment.competing_load.gpu_utilization_pct", minimum=0, maximum=100)
    _number(load["memory_used_gb"], "manifest.environment.competing_load.memory_used_gb", minimum=0)

    timing = _object(manifest["timing_scope"], "manifest.timing_scope")
    timing_bools = {"provisioning_included", "queue_included", "transfer_in_included", "transfer_out_included"}
    _exact(timing, timing_bools | {"clock", "start_boundary", "end_boundary"}, "manifest.timing_scope")
    for field in sorted(timing_bools):
        if not _boolean(timing[field], f"manifest.timing_scope.{field}"):
            raise ContractError(f"manifest.timing_scope.{field} must be true for end-to-end proof")
    for field in ("clock", "start_boundary", "end_boundary"):
        _string(timing[field], f"manifest.timing_scope.{field}", pinned=True)

    sampling = _object(manifest["sampling"], "manifest.sampling")
    _exact(sampling, {"warmup_iterations", "measured_iterations", "order", "seed"}, "manifest.sampling")
    _integer(sampling["warmup_iterations"], "manifest.sampling.warmup_iterations")
    _integer(sampling["measured_iterations"], "manifest.sampling.measured_iterations", minimum=20)
    _enum(sampling["order"], {"interleaved_ab_ba", "baseline_then_candidate", "candidate_then_baseline"}, "manifest.sampling.order")
    _integer(sampling["seed"], "manifest.sampling.seed")

    policy = _object(manifest["regression_policy"], "manifest.regression_policy")
    _exact(
        policy,
        {"min_throughput_ratio", "max_p95_latency_ratio", "max_p99_latency_ratio", "max_oom_events", "max_restart_events", "max_disconnect_events", "max_corrupt_output_events", "max_cleanup_failures", "require_cleanup"},
        "manifest.regression_policy",
    )
    _number(policy["min_throughput_ratio"], "manifest.regression_policy.min_throughput_ratio", minimum=0.000001)
    _number(policy["max_p95_latency_ratio"], "manifest.regression_policy.max_p95_latency_ratio", minimum=1)
    _number(policy["max_p99_latency_ratio"], "manifest.regression_policy.max_p99_latency_ratio", minimum=1)
    for field in ("max_oom_events", "max_restart_events", "max_disconnect_events", "max_corrupt_output_events", "max_cleanup_failures"):
        _integer(policy[field], f"manifest.regression_policy.{field}")
    if not _boolean(policy["require_cleanup"], "manifest.regression_policy.require_cleanup"):
        raise ContractError("manifest.regression_policy.require_cleanup must be true")

    if evidence_class == "physical":
        physical_values = [hardware["sku"], power["measurement_source"], thermal["sensor"]]
        if any(marker in value.lower() for value in physical_values for marker in ("synthetic", "mock", "fixture")):
            raise ContractError("physical manifest contains a synthetic/mock/fixture hardware or sensor label")
        if thermal["temperature_c"] is None:
            raise ContractError("physical manifest must pin a starting temperature")

    normalized = copy.deepcopy(manifest)
    normalized["allowed_differences"] = sorted(normalized["allowed_differences"])
    normalized["runtime"]["toolchain"] = sorted(
        normalized["runtime"]["toolchain"], key=lambda row: (row["name"], row["version"])
    )
    return normalized


def manifest_sha256(manifest: Mapping[str, Any]) -> str:
    return sha256_json(validate_manifest(manifest))


def _without_path(value: dict[str, Any], dotted_path: str) -> None:
    parts = dotted_path.split(".")
    cursor: dict[str, Any] = value
    for part in parts[:-1]:
        child = cursor.get(part)
        if not isinstance(child, dict):
            return
        cursor = child
    cursor.pop(parts[-1], None)


def _first_difference(left: Any, right: Any, path: str = "manifest") -> str:
    if type(left) is not type(right):
        return path
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                return f"{path}.{key}"
            difference = _first_difference(left[key], right[key], f"{path}.{key}")
            if difference:
                return difference
        return ""
    if isinstance(left, list):
        if len(left) != len(right):
            return path
        for index, (lhs, rhs) in enumerate(zip(left, right)):
            difference = _first_difference(lhs, rhs, f"{path}[{index}]")
            if difference:
                return difference
        return ""
    return "" if left == right else path


def compare_manifests(
    baseline_value: Mapping[str, Any], candidate_value: Mapping[str, Any]
) -> dict[str, Any]:
    baseline = validate_manifest(baseline_value)
    candidate = validate_manifest(candidate_value)
    if baseline["arm"] != "baseline" or candidate["arm"] != "candidate":
        raise IncomparableManifestError("comparison requires baseline and candidate arm roles")
    if baseline["manifest_id"] == candidate["manifest_id"]:
        raise IncomparableManifestError("baseline and candidate manifest_id must differ")
    if baseline["allowed_differences"] != candidate["allowed_differences"]:
        raise IncomparableManifestError("allowed_differences must match exactly")

    baseline_common = copy.deepcopy(baseline)
    candidate_common = copy.deepcopy(candidate)
    for row in (baseline_common, candidate_common):
        row.pop("manifest_id")
        row.pop("arm")
        for path in baseline["allowed_differences"]:
            _without_path(row, path)
    difference = _first_difference(baseline_common, candidate_common)
    if difference:
        raise IncomparableManifestError(f"manifests differ outside declared axes at {difference}")

    declared_changed = [
        path
        for path in baseline["allowed_differences"]
        if _value_at_path(baseline, path) != _value_at_path(candidate, path)
    ]
    if not declared_changed:
        raise IncomparableManifestError("no declared experimental axis actually differs")
    return {
        "comparison_id": baseline["comparison_id"],
        "baseline_lane_id": baseline["lane_id"],
        "candidate_lane_id": candidate["lane_id"],
        "comparison_scope": (
            "same_lane" if baseline["lane_id"] == candidate["lane_id"] else "cross_substrate"
        ),
        "evidence_class": baseline["evidence_class"],
        "baseline_manifest_sha256": sha256_json(baseline),
        "candidate_manifest_sha256": sha256_json(candidate),
        "comparison_fingerprint": sha256_json(baseline_common),
        "declared_differences": baseline["allowed_differences"],
        "changed_differences": declared_changed,
    }


def _value_at_path(value: Mapping[str, Any], dotted_path: str) -> Any:
    current: Any = value
    for part in dotted_path.split("."):
        current = current[part]
    return current


def validate_observations(
    value: Mapping[str, Any], manifest_value: Mapping[str, Any]
) -> dict[str, Any]:
    manifest = validate_manifest(manifest_value)
    observations = _object(value, "observations")
    _exact(
        observations,
        {"schema_version", "observation_id", "manifest_id", "manifest_sha256", "comparison_id", "arm", "lane_id", "evidence_class", "samples", "events", "cleanup"},
        "observations",
    )
    if type(observations["schema_version"]) is not int or observations["schema_version"] != 1:
        raise ContractError("observations.schema_version must be integer 1")
    _identifier(observations["observation_id"], "observations.observation_id")
    expected_bindings = {
        "manifest_id": manifest["manifest_id"],
        "manifest_sha256": sha256_json(manifest),
        "comparison_id": manifest["comparison_id"],
        "arm": manifest["arm"],
        "lane_id": manifest["lane_id"],
        "evidence_class": manifest["evidence_class"],
    }
    for field, expected in expected_bindings.items():
        if observations[field] != expected:
            raise ContractError(f"observations.{field} does not bind to manifest (want {expected!r})")

    samples = observations["samples"]
    measured = manifest["sampling"]["measured_iterations"]
    if not isinstance(samples, list) or len(samples) != measured:
        raise ContractError(f"observations.samples must contain exactly {measured} measured samples")
    sample_ids: set[str] = set()
    for index, candidate in enumerate(samples):
        context = f"observations.samples[{index}]"
        sample = _object(candidate, context)
        _exact(
            sample,
            {"sample_id", "batch_size", "completed_units", "provisioning_ms", "queue_ms", "transfer_in_ms", "compute_ms", "transfer_out_ms", "output_sha256", "expected_output_sha256", "thermal_state", "power_watts", "competing_load_snapshot_sha256"},
            context,
        )
        sample_id = _identifier(sample["sample_id"], f"{context}.sample_id")
        if sample_id in sample_ids:
            raise ContractError(f"duplicate sample_id: {sample_id}")
        sample_ids.add(sample_id)
        observed_batch = _integer(sample["batch_size"], f"{context}.batch_size", minimum=1)
        batch_policy = manifest["workload"]["batch"]
        if observed_batch > batch_policy["max_batch_size"]:
            raise ContractError(f"{context}.batch_size exceeds the pinned maximum")
        if batch_policy["mode"] == "fixed" and observed_batch != batch_policy["batch_size"]:
            raise ContractError(f"{context}.batch_size differs from the pinned fixed batch")
        completed_units = _integer(sample["completed_units"], f"{context}.completed_units", minimum=1)
        expected_units = (
            observed_batch
            * manifest["workload"]["output"]["requested_tokens_per_prompt"]
        )
        if completed_units != expected_units:
            raise ContractError(
                f"{context}.completed_units must equal batch_size × requested output tokens "
                f"({expected_units})"
            )
        for field in ("provisioning_ms", "queue_ms", "transfer_in_ms", "compute_ms", "transfer_out_ms"):
            _number(sample[field], f"{context}.{field}", minimum=0)
        if sum(sample[field] for field in ("provisioning_ms", "queue_ms", "transfer_in_ms", "compute_ms", "transfer_out_ms")) <= 0:
            raise ContractError(f"{context} has zero end-to-end duration")
        _sha256(sample["output_sha256"], f"{context}.output_sha256")
        _sha256(sample["expected_output_sha256"], f"{context}.expected_output_sha256")
        _string(sample["thermal_state"], f"{context}.thermal_state", pinned=True)
        _number(sample["power_watts"], f"{context}.power_watts", minimum=0, nullable=True)
        _sha256(sample["competing_load_snapshot_sha256"], f"{context}.competing_load_snapshot_sha256")
        if sample["thermal_state"] != manifest["environment"]["thermal"]["state"]:
            raise ContractError(f"{context}.thermal_state differs from pinned manifest state")
        if sample["competing_load_snapshot_sha256"] != manifest["environment"]["competing_load"]["process_snapshot_sha256"]:
            raise ContractError(f"{context}.competing_load_snapshot_sha256 differs from pinned manifest snapshot")
        limit = manifest["environment"]["power"]["limit_watts"]
        if limit is not None and sample["power_watts"] is not None and sample["power_watts"] > limit:
            raise ContractError(f"{context}.power_watts exceeds pinned power limit")
        if manifest["evidence_class"] == "physical" and sample["power_watts"] is None:
            raise ContractError(f"{context}.power_watts is required for physical evidence")

    events = observations["events"]
    if not isinstance(events, list):
        raise ContractError("observations.events must be a list")
    for index, candidate in enumerate(events):
        context = f"observations.events[{index}]"
        event = _object(candidate, context)
        _exact(event, {"type", "sample_id", "evidence_sha256"}, context)
        _enum(event["type"], EVENT_TYPES, f"{context}.type")
        if event["sample_id"] is not None:
            _identifier(event["sample_id"], f"{context}.sample_id")
            if event["sample_id"] not in sample_ids:
                raise ContractError(f"{context}.sample_id references an unknown sample")
        _sha256(event["evidence_sha256"], f"{context}.evidence_sha256")

    cleanup = _object(observations["cleanup"], "observations.cleanup")
    _exact(cleanup, {"attempted", "succeeded", "resources_before", "resources_after", "evidence_sha256"}, "observations.cleanup")
    _boolean(cleanup["attempted"], "observations.cleanup.attempted")
    _boolean(cleanup["succeeded"], "observations.cleanup.succeeded")
    _integer(cleanup["resources_before"], "observations.cleanup.resources_before")
    _integer(cleanup["resources_after"], "observations.cleanup.resources_after")
    _sha256(cleanup["evidence_sha256"], "observations.cleanup.evidence_sha256")
    return copy.deepcopy(observations)


def percentile(values: Sequence[float], percentile_value: float) -> float:
    if not values:
        raise ContractError("cannot compute a percentile without samples")
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(percentile_value / 100 * len(ordered))))
    return ordered[rank - 1]


def summarize_observations(
    observations_value: Mapping[str, Any], manifest_value: Mapping[str, Any]
) -> dict[str, Any]:
    observations = validate_observations(observations_value, manifest_value)
    component_fields = ("provisioning_ms", "queue_ms", "transfer_in_ms", "compute_ms", "transfer_out_ms")
    totals = {field: sum(sample[field] for sample in observations["samples"]) for field in component_fields}
    durations = [sum(sample[field] for field in component_fields) for sample in observations["samples"]]
    total_duration = sum(durations)
    completed_units = sum(sample["completed_units"] for sample in observations["samples"])
    event_counts = {event_type: 0 for event_type in sorted(EVENT_TYPES)}
    for event in observations["events"]:
        event_counts[event["type"]] += 1
    corrupt = sum(
        sample["output_sha256"] != sample["expected_output_sha256"]
        for sample in observations["samples"]
    )
    cleanup = observations["cleanup"]
    cleanup_failures = int(
        not cleanup["attempted"] or not cleanup["succeeded"] or cleanup["resources_after"] != 0
    )
    return {
        "observation_id": observations["observation_id"],
        "sample_count": len(observations["samples"]),
        "completed_units": completed_units,
        "component_totals_ms": totals,
        "end_to_end_total_ms": round(total_duration, 6),
        "throughput_units_per_second": round(completed_units * 1000 / total_duration, 6),
        "p95_end_to_end_latency_ms": round(percentile(durations, 95), 6),
        "p99_end_to_end_latency_ms": round(percentile(durations, 99), 6),
        "oom_events": event_counts["oom"],
        "restart_events": event_counts["restart"],
        "disconnect_events": event_counts["disconnect"],
        "corrupt_output_events": corrupt,
        "cleanup_failures": cleanup_failures,
    }


def _assertion(assertion_id: str, actual: Any, operator: str, threshold: Any, passed: bool) -> dict[str, Any]:
    return {
        "id": assertion_id,
        "actual": actual,
        "operator": operator,
        "threshold": threshold,
        "passed": bool(passed),
    }


def evaluate_pair(
    baseline_manifest_value: Mapping[str, Any],
    candidate_manifest_value: Mapping[str, Any],
    baseline_observations_value: Mapping[str, Any],
    candidate_observations_value: Mapping[str, Any],
) -> dict[str, Any]:
    comparison = compare_manifests(baseline_manifest_value, candidate_manifest_value)
    baseline_manifest = validate_manifest(baseline_manifest_value)
    candidate_manifest = validate_manifest(candidate_manifest_value)
    baseline = summarize_observations(baseline_observations_value, baseline_manifest)
    candidate = summarize_observations(candidate_observations_value, candidate_manifest)
    policy = baseline_manifest["regression_policy"]
    throughput_ratio = candidate["throughput_units_per_second"] / baseline["throughput_units_per_second"]
    p95_ratio = candidate["p95_end_to_end_latency_ms"] / baseline["p95_end_to_end_latency_ms"]
    p99_ratio = candidate["p99_end_to_end_latency_ms"] / baseline["p99_end_to_end_latency_ms"]
    assertions = [
        _assertion("throughput_ratio", round(throughput_ratio, 6), ">=", policy["min_throughput_ratio"], throughput_ratio >= policy["min_throughput_ratio"]),
        _assertion("p95_latency_ratio", round(p95_ratio, 6), "<=", policy["max_p95_latency_ratio"], p95_ratio <= policy["max_p95_latency_ratio"]),
        _assertion("p99_latency_ratio", round(p99_ratio, 6), "<=", policy["max_p99_latency_ratio"], p99_ratio <= policy["max_p99_latency_ratio"]),
    ]
    fault_policy_fields = {
        "oom_events": "max_oom_events",
        "restart_events": "max_restart_events",
        "disconnect_events": "max_disconnect_events",
        "corrupt_output_events": "max_corrupt_output_events",
        "cleanup_failures": "max_cleanup_failures",
    }
    for arm_name, summary in (("baseline", baseline), ("candidate", candidate)):
        for metric, policy_field in fault_policy_fields.items():
            actual = summary[metric]
            threshold = policy[policy_field]
            assertions.append(
                _assertion(f"{arm_name}.{metric}", actual, "<=", threshold, actual <= threshold)
            )
    passed = all(row["passed"] for row in assertions)
    synthetic = comparison["evidence_class"] == "synthetic"
    return {
        "schema_version": 1,
        "proof_kind": "per_lane_performance_regression_evaluation",
        "status": "PASS" if passed else "FAIL",
        "baseline_lane_id": comparison["baseline_lane_id"],
        "candidate_lane_id": comparison["candidate_lane_id"],
        "comparison_scope": comparison["comparison_scope"],
        "comparison_id": comparison["comparison_id"],
        "comparison_fingerprint": comparison["comparison_fingerprint"],
        "baseline_manifest_sha256": comparison["baseline_manifest_sha256"],
        "candidate_manifest_sha256": comparison["candidate_manifest_sha256"],
        "declared_differences": comparison["declared_differences"],
        "changed_differences": comparison["changed_differences"],
        "evidence_class": comparison["evidence_class"],
        "physical_evidence": not synthetic,
        "evidence_scope": "SYNTHETIC_ORCHESTRATION_ONLY" if synthetic else "PHYSICAL_MEASUREMENT_INGEST",
        "performance_gate_status": "NOT_PROVEN_BY_SYNTHETIC_FIXTURE" if synthetic else "REQUIRES_EXTERNAL_REVIEW",
        "baseline": baseline,
        "candidate": candidate,
        "ratios": {
            "throughput": round(throughput_ratio, 6),
            "p95_latency": round(p95_ratio, 6),
            "p99_latency": round(p99_ratio, 6),
        },
        "assertions": assertions,
    }


def build_fixture_proof() -> dict[str, Any]:
    baseline_manifest = load_json(DEFAULT_BASELINE_MANIFEST)
    candidate_manifest = load_json(DEFAULT_CANDIDATE_MANIFEST)
    baseline_observations = load_json(DEFAULT_BASELINE_OBSERVATIONS)
    pass_observations = load_json(DEFAULT_PASS_OBSERVATIONS)
    fault_observations = load_json(DEFAULT_FAULT_OBSERVATIONS)
    passing = evaluate_pair(
        baseline_manifest, candidate_manifest, baseline_observations, pass_observations
    )
    failing = evaluate_pair(
        baseline_manifest, candidate_manifest, baseline_observations, fault_observations
    )
    required_fault_assertions = {
        "throughput_ratio",
        "p95_latency_ratio",
        "p99_latency_ratio",
        "candidate.oom_events",
        "candidate.restart_events",
        "candidate.disconnect_events",
        "candidate.corrupt_output_events",
        "candidate.cleanup_failures",
    }
    observed_failures = {row["id"] for row in failing["assertions"] if not row["passed"]}
    assertions = [
        {"id": "synthetic_pass_fixture_accepted", "passed": passing["status"] == "PASS"},
        {"id": "synthetic_fault_fixture_rejected", "passed": failing["status"] == "FAIL"},
        {
            "id": "all_required_regression_and_fault_classes_detected",
            "passed": required_fault_assertions <= observed_failures,
            "required": sorted(required_fault_assertions),
            "observed": sorted(observed_failures),
        },
        {
            "id": "synthetic_never_claimed_as_physical",
            "passed": not passing["physical_evidence"]
            and passing["performance_gate_status"] == "NOT_PROVEN_BY_SYNTHETIC_FIXTURE",
        },
    ]
    return {
        "schema_version": 1,
        "proof_kind": "synthetic_performance_evaluator_contract",
        "contract_status": "PASS" if all(row["passed"] for row in assertions) else "FAIL",
        "evidence_class": "synthetic",
        "physical_evidence": False,
        "actual_benchmark_gate_status": "NOT_PROVEN",
        "actual_thermal_gate_status": "NOT_PROVEN",
        "actual_buyer_win_status": "NOT_PROVEN",
        "notice": "Synthetic numbers prove validator/evaluator behavior only; they are not performance evidence.",
        "schema_sha256": hashlib.sha256(DEFAULT_SCHEMA.read_bytes()).hexdigest(),
        "inputs": {
            "baseline_manifest": str(DEFAULT_BASELINE_MANIFEST.relative_to(ROOT)),
            "candidate_manifest": str(DEFAULT_CANDIDATE_MANIFEST.relative_to(ROOT)),
            "baseline_observations": str(DEFAULT_BASELINE_OBSERVATIONS.relative_to(ROOT)),
            "pass_observations": str(DEFAULT_PASS_OBSERVATIONS.relative_to(ROOT)),
            "fault_observations": str(DEFAULT_FAULT_OBSERVATIONS.relative_to(ROOT)),
        },
        "assertions": assertions,
        "passing_scenario": passing,
        "fault_scenario": failing,
    }


def write_or_check(path: Path, payload: Mapping[str, Any], check: bool) -> bool:
    expected = pretty_json_bytes(payload)
    if check:
        try:
            current = path.read_bytes()
        except OSError:
            current = None
        if current != expected:
            print(f"performance-proof: stale generated proof: {path}", file=sys.stderr)
            return False
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(expected)
    return True


def _add_pair_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--baseline-manifest", type=Path, required=True)
    parser.add_argument("--candidate-manifest", type=Path, required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate and compare two manifests")
    _add_pair_arguments(validate)

    run = subparsers.add_parser("run", help="ingest observations and evaluate one lane")
    _add_pair_arguments(run)
    run.add_argument("--baseline-observations", type=Path, required=True)
    run.add_argument("--candidate-observations", type=Path, required=True)
    run.add_argument("--artifact", type=Path, required=True)

    fixture = subparsers.add_parser(
        "fixture-proof", help="generate/check the deterministic synthetic contract proof"
    )
    fixture.add_argument("--output", type=Path, default=DEFAULT_PROOF)
    fixture.add_argument("--check", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            result = compare_manifests(
                load_json(args.baseline_manifest), load_json(args.candidate_manifest)
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.command == "run":
            result = evaluate_pair(
                load_json(args.baseline_manifest),
                load_json(args.candidate_manifest),
                load_json(args.baseline_observations),
                load_json(args.candidate_observations),
            )
            args.artifact.parent.mkdir(parents=True, exist_ok=True)
            args.artifact.write_bytes(pretty_json_bytes(result))
            print(
                f"performance-proof: {result['status']} "
                f"lanes={result['baseline_lane_id']}->{result['candidate_lane_id']} "
                f"evidence={result['evidence_scope']} artifact={args.artifact}"
            )
            return 0 if result["status"] == "PASS" else 1
        if args.command == "fixture-proof":
            proof = build_fixture_proof()
            if not write_or_check(args.output, proof, args.check):
                return 1
            action = "checked" if args.check else "wrote"
            print(
                f"performance-proof: {action} synthetic evaluator contract "
                f"({proof['contract_status']}; physical gates NOT PROVEN)"
            )
            return 0 if proof["contract_status"] == "PASS" else 1
    except (ContractError, OSError, json.JSONDecodeError) as exc:
        print(f"performance-proof: {exc}", file=sys.stderr)
        return 2
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())
