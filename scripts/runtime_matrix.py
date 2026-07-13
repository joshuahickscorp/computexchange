#!/usr/bin/env python3
"""Validate and deterministically project the canonical runtime capability matrix.

The generated Go projection is now fail-closed production authority for buyer
job/model admission, public catalog exposure, normalized worker registration, and
exact scheduler/routing eligibility, generated agent advertisement, and bound
dispatch/receipt provenance. Attestation and physical promotion evidence remain
explicit blockers; the overall gate therefore stays NOT_PROVEN.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = ROOT / "proto" / "runtime-matrix.source.json"
OUTPUT_PATHS = (
    "agent/src/runtime_matrix_generated.rs",
    "control/runtime_matrix_generated.go",
    "docs/RUNTIME_MATRIX.md",
    "proof/runtime-matrix.generated.json",
    "proto/runtime-matrix.generated.json",
)
SUPPORTED_LIFECYCLES = (
    "production",
    "hardware_pending",
    "soak_only",
    "stub",
    "wire_only",
    "disabled",
)
SUPPORTED_WIRE_MODEL_KINDS = ("gguf", "hf", "mlx")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class MatrixValidationError(ValueError):
    """The canonical source violates the runtime-matrix contract."""


def _duplicate_safe_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MatrixValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_source(path: Path | str = DEFAULT_SOURCE) -> dict[str, Any]:
    source_path = Path(path)
    try:
        with source_path.open("r", encoding="utf-8") as handle:
            value = json.load(handle, object_pairs_hook=_duplicate_safe_object)
    except (OSError, json.JSONDecodeError) as exc:
        raise MatrixValidationError(f"cannot read {source_path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MatrixValidationError("source root must be a JSON object")
    return value


def _require_object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MatrixValidationError(f"{context} must be an object")
    return value


def _require_exact_keys(
    value: Mapping[str, Any], required: set[str], context: str
) -> None:
    present = set(value)
    missing = sorted(required - present)
    unknown = sorted(present - required)
    if missing:
        raise MatrixValidationError(f"{context} missing field(s): {', '.join(missing)}")
    if unknown:
        raise MatrixValidationError(f"{context} has unknown field(s): {', '.join(unknown)}")


def _require_string(value: Any, context: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        qualifier = "a string" if allow_empty else "a non-empty string"
        raise MatrixValidationError(f"{context} must be {qualifier}")
    return value


def _require_id(value: Any, context: str) -> str:
    identifier = _require_string(value, context)
    if not ID_RE.fullmatch(identifier):
        raise MatrixValidationError(f"{context} has invalid identifier {identifier!r}")
    return identifier


def _require_number(value: Any, context: str, *, nullable: bool = False) -> float | int | None:
    if nullable and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MatrixValidationError(f"{context} must be a number" + (" or null" if nullable else ""))
    if value < 0:
        raise MatrixValidationError(f"{context} must be non-negative")
    return value


def _require_string_list(value: Any, context: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        qualifier = "a non-empty list" if nonempty else "a list"
        raise MatrixValidationError(f"{context} must be {qualifier} of strings")
    result: list[str] = []
    for index, item in enumerate(value):
        result.append(_require_string(item, f"{context}[{index}]"))
    if len(result) != len(set(result)):
        raise MatrixValidationError(f"{context} contains duplicate values")
    return result


def _require_lifecycle(value: Any, context: str) -> str:
    lifecycle = _require_string(value, context)
    if lifecycle not in SUPPORTED_LIFECYCLES:
        raise MatrixValidationError(f"{context} has unknown lifecycle {lifecycle!r}")
    return lifecycle


def _indexed_entries(
    source: Mapping[str, Any], key: str, fields: set[str]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    raw = source.get(key)
    if not isinstance(raw, list):
        raise MatrixValidationError(f"{key} must be a list")
    entries: list[dict[str, Any]] = []
    index: dict[str, dict[str, Any]] = {}
    for position, candidate in enumerate(raw):
        context = f"{key}[{position}]"
        entry = _require_object(candidate, context)
        _require_exact_keys(entry, fields, context)
        identifier = _require_id(entry.get("id"), f"{context}.id")
        if identifier in index:
            raise MatrixValidationError(f"duplicate {key} id: {identifier}")
        entries.append(entry)
        index[identifier] = entry
    return entries, index


def validate_source(source: Mapping[str, Any]) -> dict[str, Any]:
    """Validate source and return a canonical, order-independent deep copy."""

    root = _require_object(source, "source")
    _require_exact_keys(
        root,
        {
            "schema_version",
            "matrix_version",
            "lifecycle_states",
            "jobs",
            "models",
            "runtimes",
            "cells",
            "phase2_blockers",
        },
        "source",
    )
    if root["schema_version"] != 1 or isinstance(root["schema_version"], bool):
        raise MatrixValidationError("schema_version must be integer 1")
    _require_string(root["matrix_version"], "matrix_version")

    lifecycles = _require_string_list(root["lifecycle_states"], "lifecycle_states", nonempty=True)
    if set(lifecycles) != set(SUPPORTED_LIFECYCLES):
        missing = sorted(set(SUPPORTED_LIFECYCLES) - set(lifecycles))
        unknown = sorted(set(lifecycles) - set(SUPPORTED_LIFECYCLES))
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise MatrixValidationError("lifecycle_states mismatch: " + "; ".join(details))

    jobs, jobs_by_id = _indexed_entries(
        root, "jobs", {"id", "lifecycle", "model_required"}
    )
    models, models_by_id = _indexed_entries(
        root,
        "models",
        {
            "id",
            "family",
            "kind",
            "wire_kind",
            "quant",
            "min_memory_gb",
            "hf_repo",
            "price_per_1k",
            "price_per_unit",
            "db_catalog",
            "lifecycle",
        },
    )
    runtimes, runtimes_by_id = _indexed_entries(
        root,
        "runtimes",
        {
            "id",
            "engine",
            "device",
            "hardware_classes",
            "build_feature",
            "lifecycle",
            "description",
        },
    )
    cells, _cells_by_id = _indexed_entries(
        root,
        "cells",
        {
            "id",
            "runtime",
            "job",
            "model",
            "runner",
            "lifecycle",
            "min_memory_gb",
            "verification",
            "evidence",
            "reason",
        },
    )

    for job in jobs:
        context = f"job {job['id']}"
        _require_lifecycle(job["lifecycle"], f"{context}.lifecycle")
        if not isinstance(job["model_required"], bool):
            raise MatrixValidationError(f"{context}.model_required must be boolean")

    for model in models:
        context = f"model {model['id']}"
        _require_string(model["family"], f"{context}.family")
        _require_string(model["kind"], f"{context}.kind")
        wire_kind = _require_string(model["wire_kind"], f"{context}.wire_kind")
        if wire_kind not in SUPPORTED_WIRE_MODEL_KINDS:
            raise MatrixValidationError(
                f"{context}.wire_kind has unsupported agent wire value {wire_kind!r}"
            )
        if model["quant"] is not None:
            _require_string(model["quant"], f"{context}.quant")
        _require_number(model["min_memory_gb"], f"{context}.min_memory_gb")
        _require_string(model["hf_repo"], f"{context}.hf_repo", allow_empty=True)
        _require_number(model["price_per_1k"], f"{context}.price_per_1k", nullable=True)
        _require_number(model["price_per_unit"], f"{context}.price_per_unit", nullable=True)
        if not isinstance(model["db_catalog"], bool):
            raise MatrixValidationError(f"{context}.db_catalog must be boolean")
        lifecycle = _require_lifecycle(model["lifecycle"], f"{context}.lifecycle")
        if lifecycle == "production":
            if not model["db_catalog"]:
                raise MatrixValidationError(f"{context} is production but absent from the DB catalog")
            if not model["hf_repo"].strip():
                raise MatrixValidationError(f"{context} is production but has no model repository")

    for runtime in runtimes:
        context = f"runtime {runtime['id']}"
        _require_string(runtime["engine"], f"{context}.engine")
        _require_string(runtime["device"], f"{context}.device")
        hardware = _require_string_list(runtime["hardware_classes"], f"{context}.hardware_classes")
        if runtime["build_feature"] is not None:
            _require_string(runtime["build_feature"], f"{context}.build_feature")
        lifecycle = _require_lifecycle(runtime["lifecycle"], f"{context}.lifecycle")
        _require_string(runtime["description"], f"{context}.description")
        if lifecycle == "production" and not hardware:
            raise MatrixValidationError(f"{context} is production but has no hardware class")

    tuples: set[tuple[str, str, str | None]] = set()
    production_jobs: set[str] = set()
    production_models: set[str] = set()
    production_runtimes: set[str] = set()
    for cell in cells:
        context = f"cell {cell['id']}"
        runtime_id = _require_id(cell["runtime"], f"{context}.runtime")
        job_id = _require_id(cell["job"], f"{context}.job")
        model_id = cell["model"]
        if model_id is not None:
            model_id = _require_id(model_id, f"{context}.model")
        if runtime_id not in runtimes_by_id:
            raise MatrixValidationError(f"{context} references unknown runtime {runtime_id!r}")
        if job_id not in jobs_by_id:
            raise MatrixValidationError(f"{context} references unknown job {job_id!r}")
        if model_id is not None and model_id not in models_by_id:
            raise MatrixValidationError(f"{context} references unknown model {model_id!r}")
        tuple_key = (runtime_id, job_id, model_id)
        if tuple_key in tuples:
            raise MatrixValidationError(
                f"duplicate capability tuple: runtime={runtime_id}, job={job_id}, model={model_id}"
            )
        tuples.add(tuple_key)

        lifecycle = _require_lifecycle(cell["lifecycle"], f"{context}.lifecycle")
        if cell["runner"] is not None:
            _require_string(cell["runner"], f"{context}.runner")
        _require_number(cell["min_memory_gb"], f"{context}.min_memory_gb")
        _require_string(cell["verification"], f"{context}.verification")
        evidence = _require_string_list(cell["evidence"], f"{context}.evidence")
        _require_string(cell["reason"], f"{context}.reason")

        # Wire-only rows describe contract variants for which no instantiated
        # model tuple exists yet. Every runnable maturity state must obey the
        # job's model requirement.
        if jobs_by_id[job_id]["model_required"] and model_id is None and lifecycle not in {
            "wire_only",
            "disabled",
        }:
            raise MatrixValidationError(f"{context} requires a model reference")

        if lifecycle == "production":
            if runtimes_by_id[runtime_id]["lifecycle"] != "production":
                raise MatrixValidationError(f"{context} is production on a non-production runtime")
            if jobs_by_id[job_id]["lifecycle"] != "production":
                raise MatrixValidationError(f"{context} is production for a non-production job")
            if model_id is not None and models_by_id[model_id]["lifecycle"] != "production":
                raise MatrixValidationError(f"{context} is production for a non-production model")
            if cell["runner"] is None:
                raise MatrixValidationError(f"{context} is production but has no runner")
            if cell["verification"] == "none":
                raise MatrixValidationError(f"{context} is production but has no verification")
            if not evidence:
                raise MatrixValidationError(f"{context} is production but has no evidence")
            production_jobs.add(job_id)
            production_runtimes.add(runtime_id)
            if model_id is not None:
                production_models.add(model_id)

    declared_production_jobs = {row["id"] for row in jobs if row["lifecycle"] == "production"}
    declared_production_models = {row["id"] for row in models if row["lifecycle"] == "production"}
    declared_production_runtimes = {row["id"] for row in runtimes if row["lifecycle"] == "production"}
    for label, declared, used in (
        ("job", declared_production_jobs, production_jobs),
        ("model", declared_production_models, production_models),
        ("runtime", declared_production_runtimes, production_runtimes),
    ):
        orphaned = sorted(declared - used)
        if orphaned:
            raise MatrixValidationError(
                f"production {label}(s) have no production capability cell: {', '.join(orphaned)}"
            )

    blockers = _require_string_list(root["phase2_blockers"], "phase2_blockers", nonempty=True)

    normalized = copy.deepcopy(root)
    normalized["lifecycle_states"] = list(SUPPORTED_LIFECYCLES)
    for key in ("jobs", "models", "runtimes", "cells"):
        normalized[key] = sorted(normalized[key], key=lambda row: row["id"])
    for runtime in normalized["runtimes"]:
        runtime["hardware_classes"] = sorted(runtime["hardware_classes"])
    for cell in normalized["cells"]:
        cell["evidence"] = sorted(cell["evidence"])
    normalized["phase2_blockers"] = sorted(blockers)
    return normalized


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def pretty_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _lifecycle_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {
        lifecycle: sum(row["lifecycle"] == lifecycle for row in rows)
        for lifecycle in SUPPORTED_LIFECYCLES
    }


def build_matrix(source: Mapping[str, Any]) -> dict[str, Any]:
    """Expand validated source with derived, non-Cartesian projections and digests."""

    normalized = validate_source(source)
    runtimes = {row["id"]: row for row in normalized["runtimes"]}
    models = {row["id"]: row for row in normalized["models"]}
    expanded_cells: list[dict[str, Any]] = []
    for row in normalized["cells"]:
        cell = copy.deepcopy(row)
        runtime = runtimes[cell["runtime"]]
        cell["engine"] = runtime["engine"]
        cell["device"] = runtime["device"]
        cell["hardware_classes"] = copy.deepcopy(runtime["hardware_classes"])
        cell["model_kind"] = (
            models[cell["model"]]["wire_kind"] if cell["model"] is not None else None
        )
        cell["advertised"] = cell["lifecycle"] == "production"
        expanded_cells.append(cell)

    advertised = [cell for cell in expanded_cells if cell["advertised"]]
    by_runtime: list[dict[str, Any]] = []
    for runtime_id in sorted({cell["runtime"] for cell in advertised}):
        tuples = [cell for cell in advertised if cell["runtime"] == runtime_id]
        runtime = runtimes[runtime_id]
        by_runtime.append(
            {
                "runtime": runtime_id,
                "engine": runtime["engine"],
                "device": runtime["device"],
                "hardware_classes": copy.deepcopy(runtime["hardware_classes"]),
                "cell_ids": [cell["id"] for cell in tuples],
                "jobs": sorted({cell["job"] for cell in tuples}),
                "models": sorted({cell["model"] for cell in tuples if cell["model"] is not None}),
            }
        )

    matrix: dict[str, Any] = {
        "schema_version": normalized["schema_version"],
        "matrix_version": normalized["matrix_version"],
        "source_sha256": sha256_json(normalized),
        "lifecycle_states": normalized["lifecycle_states"],
        "jobs": normalized["jobs"],
        "models": normalized["models"],
        "runtimes": normalized["runtimes"],
        "cells": expanded_cells,
        "advertised_projection": {
            "authorization_shape": "explicit_runtime_job_model_tuples_only",
            "compatibility_rollups_are_not_authorization": True,
            "cell_ids": [cell["id"] for cell in advertised],
            "tuples": [
                {
                    "cell_id": cell["id"],
                    "runtime": cell["runtime"],
                    "engine": cell["engine"],
                    "device": cell["device"],
                    "hardware_classes": cell["hardware_classes"],
                    "job": cell["job"],
                    "model": cell["model"],
                    "model_kind": cell["model_kind"],
                    "runner": cell["runner"],
                    "min_memory_gb": cell["min_memory_gb"],
                    "verification": cell["verification"],
                }
                for cell in advertised
            ],
            "jobs": sorted({cell["job"] for cell in advertised}),
            "models": sorted({cell["model"] for cell in advertised if cell["model"] is not None}),
            "runtimes": sorted({cell["runtime"] for cell in advertised}),
            "by_runtime": by_runtime,
        },
        "db_catalog_projection": [
            copy.deepcopy(model) for model in normalized["models"] if model["db_catalog"]
        ],
        "counts": {
            "jobs": _lifecycle_counts(normalized["jobs"]),
            "models": _lifecycle_counts(normalized["models"]),
            "runtimes": _lifecycle_counts(normalized["runtimes"]),
            "cells": _lifecycle_counts(expanded_cells),
        },
        "enforcement": {
            "phase": "tranche_4_bound_execution_authority",
            "gate_proven": False,
            "production_consumes_generated_projection": True,
            "registration_persists_exact_worker_cells": True,
            "scheduler_requires_exact_worker_cells": True,
            "agent_advertisement_consumes_generated_projection": True,
            "dispatch_and_receipts_bind_exact_cells": True,
            "dispatch_carries_generated_model_kind": True,
            "legacy_array_workers_backfilled": False,
            "next_phase": "attestation_and_physical_promotion_evidence",
        },
        "phase2_blockers": normalized["phase2_blockers"],
    }
    matrix["matrix_sha256"] = sha256_json(matrix)
    return matrix


def build_report(matrix: Mapping[str, Any]) -> dict[str, Any]:
    nonproduction = {
        lifecycle: [
            cell["id"] for cell in matrix["cells"] if cell["lifecycle"] == lifecycle
        ]
        for lifecycle in SUPPORTED_LIFECYCLES
        if lifecycle != "production"
    }
    return {
        "schema_version": matrix["schema_version"],
        "matrix_version": matrix["matrix_version"],
        "source_sha256": matrix["source_sha256"],
        "matrix_sha256": matrix["matrix_sha256"],
        "status": "NOT_PROVEN",
        "phase": matrix["enforcement"]["phase"],
        "gate_proven": False,
        "production_consumes_generated_projection": matrix["enforcement"][
            "production_consumes_generated_projection"
        ],
        "registration_persists_exact_worker_cells": matrix["enforcement"][
            "registration_persists_exact_worker_cells"
        ],
        "scheduler_requires_exact_worker_cells": matrix["enforcement"][
            "scheduler_requires_exact_worker_cells"
        ],
        "agent_advertisement_consumes_generated_projection": matrix["enforcement"][
            "agent_advertisement_consumes_generated_projection"
        ],
        "dispatch_and_receipts_bind_exact_cells": matrix["enforcement"][
            "dispatch_and_receipts_bind_exact_cells"
        ],
        "dispatch_carries_generated_model_kind": matrix["enforcement"][
            "dispatch_carries_generated_model_kind"
        ],
        "legacy_array_workers_backfilled": matrix["enforcement"][
            "legacy_array_workers_backfilled"
        ],
        "advertised_cell_ids": matrix["advertised_projection"]["cell_ids"],
        "counts": matrix["counts"],
        "nonproduction_cell_ids": nonproduction,
        "phase2_blockers": matrix["phase2_blockers"],
    }


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _go_number(value: float | int) -> str:
    return format(value, ".15g")


def _rust_float(value: float | int) -> str:
    rendered = format(value, ".15g")
    return rendered if any(marker in rendered for marker in (".", "e", "E")) else rendered + ".0"


def render_go(matrix: Mapping[str, Any]) -> str:
    lines = [
        "// Code generated by scripts/runtime_matrix.py; DO NOT EDIT.",
        "// Production exact-cell admission/scheduling/receipt authority; physical proof remains open.",
        "",
        "package main",
        "",
        f"const generatedRuntimeMatrixVersion = {_quoted(matrix['matrix_version'])}",
        f"const generatedRuntimeMatrixSHA256 = {_quoted(matrix['matrix_sha256'])}",
        "const generatedRuntimeMatrixGateProven = false",
        "",
        "type generatedRuntimeCapability struct {",
        "\tID              string",
        "\tRuntime         string",
        "\tEngine          string",
        "\tDevice          string",
        "\tHardwareClasses []string",
        "\tJob             string",
        "\tModel           string",
        "\tModelKind       string",
        "\tRunner          string",
        "\tMinMemoryGB     float64",
        "\tVerification    string",
        "}",
        "",
        "var generatedAdvertisedRuntimeCapabilities = []generatedRuntimeCapability{",
    ]
    for cell in matrix["advertised_projection"]["tuples"]:
        hardware = ", ".join(_quoted(item) for item in cell["hardware_classes"])
        lines.extend(
            [
                "\t{",
                f"\t\tID:              {_quoted(cell['cell_id'])},",
                f"\t\tRuntime:         {_quoted(cell['runtime'])},",
                f"\t\tEngine:          {_quoted(cell['engine'])},",
                f"\t\tDevice:          {_quoted(cell['device'])},",
                f"\t\tHardwareClasses: []string{{{hardware}}},",
                f"\t\tJob:             {_quoted(cell['job'])},",
                f"\t\tModel:           {_quoted(cell['model'] or '')},",
                f"\t\tModelKind:       {_quoted(cell['model_kind'] or '')},",
                f"\t\tRunner:          {_quoted(cell['runner'] or '')},",
                f"\t\tMinMemoryGB:     {_go_number(cell['min_memory_gb'])},",
                f"\t\tVerification:    {_quoted(cell['verification'])},",
                "\t},",
            ]
        )
    lines.extend(
        [
            "}",
            "",
            "type generatedRuntimeCellTruth struct {",
            "\tID         string",
            "\tRuntime    string",
            "\tJob        string",
            "\tModel      string",
            "\tModelKind  string",
            "\tLifecycle  string",
            "\tAdvertised bool",
            "}",
            "",
            "var generatedRuntimeCellTruths = []generatedRuntimeCellTruth{",
        ]
    )
    for cell in matrix["cells"]:
        advertised = "true" if cell["advertised"] else "false"
        lines.append(
            "\t{"
            f"ID: {_quoted(cell['id'])}, Runtime: {_quoted(cell['runtime'])}, "
            f"Job: {_quoted(cell['job'])}, Model: {_quoted(cell['model'] or '')}, "
            f"ModelKind: {_quoted(cell['model_kind'] or '')}, "
            f"Lifecycle: {_quoted(cell['lifecycle'])}, Advertised: {advertised}"
            "},"
        )
    lines.extend(["}", "", "var generatedRuntimePhase2Blockers = []string{"])
    for blocker in matrix["phase2_blockers"]:
        lines.append(f"\t{_quoted(blocker)},")
    lines.extend(["}", ""])
    return "\n".join(lines)


def render_rust(matrix: Mapping[str, Any]) -> str:
    lines = [
        "// Code generated by scripts/runtime_matrix.py; DO NOT EDIT.",
        "// Generated production truth consumed by agent advertisement and dispatch validation.",
        "#![allow(dead_code)]",
        "",
        f"pub const RUNTIME_MATRIX_VERSION: &str = {_quoted(matrix['matrix_version'])};",
        "pub const RUNTIME_MATRIX_SHA256: &str =",
        f"    {_quoted(matrix['matrix_sha256'])};",
        "pub const RUNTIME_MATRIX_GATE_PROVEN: bool = false;",
        "",
        "#[derive(Debug, Clone, Copy)]",
        "pub struct GeneratedRuntimeCapability {",
        "    pub id: &'static str,",
        "    pub runtime: &'static str,",
        "    pub engine: &'static str,",
        "    pub device: &'static str,",
        "    pub hardware_classes: &'static [&'static str],",
        "    pub job: &'static str,",
        "    pub model: Option<&'static str>,",
        "    pub model_kind: Option<&'static str>,",
        "    pub runner: &'static str,",
        "    pub min_memory_gb: f64,",
        "    pub verification: &'static str,",
        "}",
        "",
        "pub const ADVERTISED_RUNTIME_CAPABILITIES: &[GeneratedRuntimeCapability] = &[",
    ]
    for cell in matrix["advertised_projection"]["tuples"]:
        model = "None" if cell["model"] is None else f"Some({_quoted(cell['model'])})"
        model_kind = (
            "None" if cell["model_kind"] is None else f"Some({_quoted(cell['model_kind'])})"
        )
        lines.extend(
            [
                "    GeneratedRuntimeCapability {",
                f"        id: {_quoted(cell['cell_id'])},",
                f"        runtime: {_quoted(cell['runtime'])},",
                f"        engine: {_quoted(cell['engine'])},",
                f"        device: {_quoted(cell['device'])},",
                "        hardware_classes: &[",
            ]
        )
        for hardware_class in cell["hardware_classes"]:
            lines.append(f"            {_quoted(hardware_class)},")
        lines.extend(
            [
                "        ],",
                f"        job: {_quoted(cell['job'])},",
                f"        model: {model},",
                f"        model_kind: {model_kind},",
                f"        runner: {_quoted(cell['runner'])},",
                f"        min_memory_gb: {_rust_float(cell['min_memory_gb'])},",
                f"        verification: {_quoted(cell['verification'])},",
                "    },",
            ]
        )
    lines.extend(
        [
            "];",
            "",
            "#[derive(Debug, Clone, Copy)]",
            "pub struct GeneratedRuntimeCellTruth {",
            "    pub id: &'static str,",
            "    pub runtime: &'static str,",
            "    pub job: &'static str,",
            "    pub model: Option<&'static str>,",
            "    pub model_kind: Option<&'static str>,",
            "    pub lifecycle: &'static str,",
            "    pub advertised: bool,",
            "}",
            "",
            "pub const RUNTIME_CELL_TRUTHS: &[GeneratedRuntimeCellTruth] = &[",
        ]
    )
    for cell in matrix["cells"]:
        model = "None" if cell["model"] is None else f"Some({_quoted(cell['model'])})"
        model_kind = (
            "None" if cell["model_kind"] is None else f"Some({_quoted(cell['model_kind'])})"
        )
        advertised = "true" if cell["advertised"] else "false"
        lines.extend(
            [
                "    GeneratedRuntimeCellTruth {",
                f"        id: {_quoted(cell['id'])},",
                f"        runtime: {_quoted(cell['runtime'])},",
                f"        job: {_quoted(cell['job'])},",
                f"        model: {model},",
                f"        model_kind: {model_kind},",
                f"        lifecycle: {_quoted(cell['lifecycle'])},",
                f"        advertised: {advertised},",
                "    },",
            ]
        )
    lines.extend(["];", "", "pub const PHASE2_BLOCKERS: &[&str] = &["])
    for blocker in matrix["phase2_blockers"]:
        lines.append(f"    {_quoted(blocker)},")
    lines.extend(["];", ""])
    return "\n".join(lines)


def _markdown(value: Any) -> str:
    if value is None:
        return "—"
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_docs(matrix: Mapping[str, Any]) -> str:
    lines = [
        "<!-- Code generated by scripts/runtime_matrix.py; DO NOT EDIT. -->",
        "# Runtime capability matrix",
        "",
        "This is the deterministic projection of `proto/runtime-matrix.source.json`.",
        "The control plane consumes production cells as fail-closed admission and exact worker",
        "scheduling authority; the agent derives its advertisement from the same projection,",
        "and dispatch metadata plus clearing receipts are bound to the selected exact cell.",
        "",
        "**Gate status: NOT PROVEN.** Exact server-side scheduling is enforced, while",
        "remote attestation and physical evidence for non-production cells remain open.",
        "",
        f"- Matrix version: `{matrix['matrix_version']}`",
        f"- Source SHA-256: `{matrix['source_sha256']}`",
        f"- Expanded matrix SHA-256: `{matrix['matrix_sha256']}`",
        "",
        "Regenerate with `python3 scripts/runtime_matrix.py`; verify committed outputs with "
        "`python3 scripts/runtime_matrix.py --check`.",
        "",
        "Lifecycle meanings: `production` is eligible for the advertised tuple projection; "
        "`hardware_pending` has a plausible runner but lacks release or physical proof; "
        "`soak_only` is explicitly experimental; `stub` reaches a non-executing boundary; "
        "`wire_only` is a contract or unsupported fallback; and `disabled` is intentionally unavailable.",
        "",
        "## Lifecycle summary",
        "",
        "| Entity | Production | Hardware pending | Soak only | Stub | Wire only | Disabled |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for entity in ("jobs", "models", "runtimes", "cells"):
        count = matrix["counts"][entity]
        lines.append(
            f"| {entity.title()} | {count['production']} | {count['hardware_pending']} | "
            f"{count['soak_only']} | {count['stub']} | {count['wire_only']} | {count['disabled']} |"
        )

    lines.extend(
        [
            "",
            "## Advertised production tuples",
            "",
            "Only these explicit runtime/job/model cells are advertised by this projection.",
            "The job and model roll-ups are compatibility views and must not be treated as a Cartesian product.",
            "",
            "| Cell | Runtime | Engine/device | Hardware classes | Job | Model | Wire kind | Runner | Min GB | Verification |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | ---: | --- |",
        ]
    )
    for cell in matrix["advertised_projection"]["tuples"]:
        lines.append(
            f"| `{_markdown(cell['cell_id'])}` | `{_markdown(cell['runtime'])}` | "
            f"`{_markdown(cell['engine'])}/{_markdown(cell['device'])}` | "
            f"{', '.join(_markdown(item) for item in cell['hardware_classes'])} | "
            f"`{_markdown(cell['job'])}` | `{_markdown(cell['model'])}` | "
            f"`{_markdown(cell['model_kind'])}` | "
            f"`{_markdown(cell['runner'])}` | {_markdown(cell['min_memory_gb'])} | "
            f"`{_markdown(cell['verification'])}` |"
        )

    projection = matrix["advertised_projection"]
    lines.extend(
        [
            "",
            "Compatibility roll-ups (not authorization):",
            "",
            "- Jobs: " + ", ".join(f"`{item}`" for item in projection["jobs"]),
            "- Models: " + ", ".join(f"`{item}`" for item in projection["models"]),
            "- Runtimes: " + ", ".join(f"`{item}`" for item in projection["runtimes"]),
            "",
            "## Runtime truth",
            "",
            "| Runtime | Engine | Device | Lifecycle | Hardware classes | Description |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for runtime in matrix["runtimes"]:
        lines.append(
            f"| `{runtime['id']}` | `{runtime['engine']}` | `{runtime['device']}` | "
            f"`{runtime['lifecycle']}` | {', '.join(runtime['hardware_classes']) or '—'} | "
            f"{_markdown(runtime['description'])} |"
        )

    lines.extend(
        [
            "",
            "## Complete cell truth",
            "",
            "| Cell | Runtime | Job | Model | Wire kind | Lifecycle | Advertised | Reason |",
            "| --- | --- | --- | --- | --- | --- | :---: | --- |",
        ]
    )
    for cell in matrix["cells"]:
        model_markdown = f"`{cell['model']}`" if cell["model"] is not None else "—"
        kind_markdown = f"`{cell['model_kind']}`" if cell["model_kind"] is not None else "—"
        lines.append(
            f"| `{cell['id']}` | `{cell['runtime']}` | `{cell['job']}` | "
            f"{model_markdown} | {kind_markdown} | "
            f"`{cell['lifecycle']}` | {'yes' if cell['advertised'] else 'no'} | "
            f"{_markdown(cell['reason'])} |"
        )

    lines.extend(
        [
            "",
            "## DB catalog projection",
            "",
            "This mirrors the existing catalog membership; lifecycle still controls advertisement.",
            "",
            "| Model | Catalog kind | Wire kind | Lifecycle | Minimum GB | Repository |",
            "| --- | --- | --- | --- | ---: | --- |",
        ]
    )
    for model in matrix["db_catalog_projection"]:
        lines.append(
            f"| `{model['id']}` | `{model['kind']}` | `{model['wire_kind']}` | `{model['lifecycle']}` | "
            f"{model['min_memory_gb']} | {_markdown(model['hf_repo'])} |"
        )

    lines.extend(
        [
            "",
            "## Remaining enforcement blockers",
            "",
            "The runtime-matrix gate must remain unproven until these are closed:",
            "",
        ]
    )
    lines.extend(f"- {blocker}" for blocker in matrix["phase2_blockers"])
    lines.append("")
    return "\n".join(lines)


def render_outputs(matrix: Mapping[str, Any]) -> dict[str, bytes]:
    report = build_report(matrix)
    outputs = {
        "agent/src/runtime_matrix_generated.rs": render_rust(matrix).encode("utf-8"),
        "control/runtime_matrix_generated.go": render_go(matrix).encode("utf-8"),
        "docs/RUNTIME_MATRIX.md": render_docs(matrix).encode("utf-8"),
        "proof/runtime-matrix.generated.json": pretty_json_bytes(report),
        "proto/runtime-matrix.generated.json": pretty_json_bytes(matrix),
    }
    if tuple(sorted(outputs)) != tuple(sorted(OUTPUT_PATHS)):
        raise AssertionError("generator output path contract drifted")
    return outputs


def write_outputs(output_root: Path | str, outputs: Mapping[str, bytes]) -> None:
    root = Path(output_root)
    for relative in sorted(outputs):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(outputs[relative])


def stale_outputs(output_root: Path | str, outputs: Mapping[str, bytes]) -> list[str]:
    root = Path(output_root)
    stale: list[str] = []
    for relative in sorted(outputs):
        path = root / relative
        try:
            current = path.read_bytes()
        except OSError:
            current = None
        if current != outputs[relative]:
            stale.append(relative)
    return stale


def write_artifacts(artifact_dir: Path | str, matrix: Mapping[str, Any]) -> None:
    root = Path(artifact_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "matrix.json").write_bytes(pretty_json_bytes(matrix))
    (root / "report.json").write_bytes(pretty_json_bytes(build_report(matrix)))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-root", type=Path, default=ROOT)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if committed generated outputs differ; never rewrite them",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        matrix = build_matrix(load_source(args.source))
        outputs = render_outputs(matrix)
    except MatrixValidationError as exc:
        print(f"runtime-matrix: invalid source: {exc}", file=sys.stderr)
        return 2

    if args.artifact_dir is not None:
        write_artifacts(args.artifact_dir, matrix)

    if args.check:
        stale = stale_outputs(args.output_root, outputs)
        if stale:
            print("runtime-matrix: generated outputs are stale:", file=sys.stderr)
            for relative in stale:
                print(f"  {relative}", file=sys.stderr)
            print("run: python3 scripts/runtime_matrix.py", file=sys.stderr)
            return 1
        print(
            f"runtime-matrix: check passed ({len(outputs)} outputs, "
            f"{len(matrix['advertised_projection']['cell_ids'])} advertised production cells)"
        )
        return 0

    write_outputs(args.output_root, outputs)
    print(
        f"runtime-matrix: wrote {len(outputs)} outputs "
        f"({len(matrix['advertised_projection']['cell_ids'])} advertised production cells; gate NOT PROVEN)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
