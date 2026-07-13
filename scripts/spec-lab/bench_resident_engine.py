#!/usr/bin/env python3
"""Build a bounded, non-production 20x resident-engine benchmark artifact.

This is deliberately an *ingestor and calculator*, not a benchmark generator.
It consumes two explicit JSON scenario traces, rejects comparisons whose logical
work or substrate differs, and derives scheduler/KV/speculation accounting from
the supplied counters.  Analytic times remain simulations; a timing result is
only labelled as a real wall-clock observation when both arms supply recorded
wall-clock samples.  Neither label is a production-performance claim.

The manifest binding and fail-closed comparison rules mirror the useful parts
of ``scripts/performance_proof.py`` without importing a mutable production
surface.  The schema intentionally stays small enough to be emitted by a
resident scheduler or by a deterministic simulator.

Example:

  python3 scripts/spec-lab/bench_resident_engine.py \
    --baseline baseline.json --candidate candidate.json --artifact result.json

The candidate's target ledger must include prefix reuse, continuous batching,
kernel work, speculation, and residency.  It is a plan, not proof: correlated
factors are never multiplied unless their combination has independent
real-wall-clock evidence, and a 20x result is only observed through a direct,
same-work real wall-clock comparison.
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


SCHEMA_VERSION = 1
MAX_REQUESTS = 1_000_000_000
MAX_TOKENS = 1_000_000_000_000
MAX_BATCH_WIDTH = 1_000_000
MAX_PROPOSAL_MULTIPLIER = 64
MAX_WRITE_AMPLIFICATION = 128
MAX_DURATION_MS = 86_400_000.0  # one day; a larger number is almost certainly bad input.
REQUIRED_LEDGER_LANES = (
    "prefix_reuse",
    "continuous_batching",
    "kernel",
    "speculation",
    "residency",
)
TIMING_KINDS = {"analytic_simulation", "real_wall_clock"}
TRACE_KINDS = {"analytic_simulation", "runtime_trace"}
LEDGER_EVIDENCE_KINDS = {"unproven", "analytic_simulation", "real_wall_clock"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ContractError(ValueError):
    """A scenario is malformed or tries to encode an unbounded claim."""


class IncompatibleScenarioError(ContractError):
    """Baseline and candidate do not represent the same logical experiment."""


def _duplicate_safe_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path | str) -> dict[str, Any]:
    """Load one object-valued JSON scenario with duplicate-key rejection."""

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


def _identifier(value: Any, context: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ContractError(f"{context} must be a lowercase identifier")
    return value


def _string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{context} must be a non-empty string")
    return value


def _sha256(value: Any, context: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ContractError(f"{context} must be a lowercase 64-character SHA-256")
    return value


def _integer(value: Any, context: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractError(f"{context} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise ContractError(f"{context} must be <= {maximum}")
    return value


def _number(
    value: Any,
    context: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    nullable: bool = False,
) -> float:
    if nullable and value is None:
        return None  # type: ignore[return-value]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ContractError(f"{context} must be a finite number" + (" or null" if nullable else ""))
    result = float(value)
    if minimum is not None and result < minimum:
        raise ContractError(f"{context} must be >= {minimum}")
    if maximum is not None and result > maximum:
        raise ContractError(f"{context} must be <= {maximum}")
    return result


def _boolean(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{context} must be boolean")
    return value


def _enum(value: Any, choices: set[str], context: str) -> str:
    result = _string(value, context)
    if result not in choices:
        raise ContractError(f"{context} must be one of: {', '.join(sorted(choices))}")
    return result


def _percentile(values: Sequence[int | float], percentile_value: float) -> float:
    if not values:
        raise ContractError("cannot compute a percentile without samples")
    ordered = sorted(float(value) for value in values)
    rank = max(1, min(len(ordered), math.ceil(percentile_value / 100 * len(ordered))))
    return ordered[rank - 1]


def _rounded(value: float) -> float:
    return round(value, 6)


def _validate_provenance(value: Any) -> dict[str, Any]:
    row = _object(value, "scenario.provenance")
    _exact(
        row,
        {
            "engine_build_sha256",
            "model_sha256",
            "tokenizer_sha256",
            "hardware_fingerprint_sha256",
            "workload_source_sha256",
            "trace_sha256",
            "trace_kind",
            "timing_boundary",
        },
        "scenario.provenance",
    )
    for field in (
        "engine_build_sha256",
        "model_sha256",
        "tokenizer_sha256",
        "hardware_fingerprint_sha256",
        "workload_source_sha256",
        "trace_sha256",
    ):
        _sha256(row[field], f"scenario.provenance.{field}")
    _enum(row["trace_kind"], TRACE_KINDS, "scenario.provenance.trace_kind")
    _identifier(row["timing_boundary"], "scenario.provenance.timing_boundary")
    return copy.deepcopy(row)


def _validate_logical_work(value: Any) -> dict[str, Any]:
    row = _object(value, "scenario.logical_work")
    _exact(
        row,
        {
            "corpus_sha256",
            "request_count",
            "total_prompt_tokens",
            "max_prompt_tokens",
            "total_generated_tokens",
            "output_sha256",
            "stop_conditions_sha256",
        },
        "scenario.logical_work",
    )
    for field in ("corpus_sha256", "output_sha256", "stop_conditions_sha256"):
        _sha256(row[field], f"scenario.logical_work.{field}")
    request_count = _integer(row["request_count"], "scenario.logical_work.request_count", minimum=1, maximum=MAX_REQUESTS)
    total_prompt = _integer(row["total_prompt_tokens"], "scenario.logical_work.total_prompt_tokens", maximum=MAX_TOKENS)
    max_prompt = _integer(row["max_prompt_tokens"], "scenario.logical_work.max_prompt_tokens", maximum=MAX_TOKENS)
    total_generated = _integer(row["total_generated_tokens"], "scenario.logical_work.total_generated_tokens", minimum=1, maximum=MAX_TOKENS)
    if max_prompt > total_prompt:
        raise ContractError("scenario.logical_work.max_prompt_tokens exceeds total_prompt_tokens")
    if total_prompt < max_prompt and request_count == 1:
        raise ContractError("one request must have total_prompt_tokens equal to max_prompt_tokens")
    if total_generated < request_count:
        raise ContractError("scenario.logical_work.total_generated_tokens is below one output token/request")
    return copy.deepcopy(row)


def _validate_scheduler(value: Any, work: Mapping[str, Any]) -> dict[str, Any]:
    row = _object(value, "scenario.scheduler")
    _exact(
        row,
        {
            "admissions",
            "releases",
            "dispatches",
            "prefill_dispatches",
            "decode_dispatches",
            "batch_widths",
            "max_active_slots",
            "compactions",
        },
        "scenario.scheduler",
    )
    admissions = _integer(row["admissions"], "scenario.scheduler.admissions", maximum=MAX_REQUESTS)
    releases = _integer(row["releases"], "scenario.scheduler.releases", maximum=MAX_REQUESTS)
    dispatches = _integer(row["dispatches"], "scenario.scheduler.dispatches", minimum=1, maximum=MAX_TOKENS)
    prefill = _integer(row["prefill_dispatches"], "scenario.scheduler.prefill_dispatches", maximum=MAX_TOKENS)
    decode = _integer(row["decode_dispatches"], "scenario.scheduler.decode_dispatches", maximum=MAX_TOKENS)
    max_active = _integer(row["max_active_slots"], "scenario.scheduler.max_active_slots", minimum=1, maximum=MAX_BATCH_WIDTH)
    _integer(row["compactions"], "scenario.scheduler.compactions", maximum=MAX_TOKENS)
    if admissions != work["request_count"] or releases != work["request_count"]:
        raise ContractError("scenario.scheduler admissions and releases must equal logical request_count")
    if prefill + decode != dispatches:
        raise ContractError("scenario.scheduler prefill_dispatches + decode_dispatches must equal dispatches")
    widths = row["batch_widths"]
    if not isinstance(widths, list) or len(widths) != dispatches:
        raise ContractError("scenario.scheduler.batch_widths must contain exactly dispatches entries")
    for index, width in enumerate(widths):
        _integer(width, f"scenario.scheduler.batch_widths[{index}]", minimum=1, maximum=max_active)
    return copy.deepcopy(row)


def _validate_kv(value: Any, work: Mapping[str, Any]) -> dict[str, Any]:
    row = _object(value, "scenario.kv")
    _exact(
        row,
        {
            "allocated_token_slots",
            "peak_live_token_slots",
            "live_token_slots_after",
            "prefix_reused_prompt_tokens",
            "kv_write_tokens",
            "rollback_tokens",
            "evictions",
            "max_write_amplification",
        },
        "scenario.kv",
    )
    allocated = _integer(row["allocated_token_slots"], "scenario.kv.allocated_token_slots", minimum=1, maximum=MAX_TOKENS)
    peak = _integer(row["peak_live_token_slots"], "scenario.kv.peak_live_token_slots", maximum=MAX_TOKENS)
    live_after = _integer(row["live_token_slots_after"], "scenario.kv.live_token_slots_after", maximum=MAX_TOKENS)
    reused = _integer(row["prefix_reused_prompt_tokens"], "scenario.kv.prefix_reused_prompt_tokens", maximum=work["total_prompt_tokens"])
    writes = _integer(row["kv_write_tokens"], "scenario.kv.kv_write_tokens", maximum=MAX_TOKENS)
    rollback = _integer(row["rollback_tokens"], "scenario.kv.rollback_tokens", maximum=MAX_TOKENS)
    _integer(row["evictions"], "scenario.kv.evictions", maximum=MAX_TOKENS)
    multiplier = _integer(row["max_write_amplification"], "scenario.kv.max_write_amplification", minimum=1, maximum=MAX_WRITE_AMPLIFICATION)
    if peak > allocated:
        raise ContractError("scenario.kv.peak_live_token_slots exceeds allocated_token_slots")
    if live_after != 0:
        raise ContractError("scenario.kv.live_token_slots_after must be zero after a completed comparable run")
    fresh_committed = work["total_prompt_tokens"] - reused + work["total_generated_tokens"]
    if writes < fresh_committed:
        raise ContractError("scenario.kv.kv_write_tokens is below fresh prompt + generated committed tokens")
    if writes > fresh_committed * multiplier:
        raise ContractError("scenario.kv.kv_write_tokens exceeds its explicit max_write_amplification bound")
    if rollback > writes:
        raise ContractError("scenario.kv.rollback_tokens exceeds kv_write_tokens")
    return copy.deepcopy(row)


def _validate_speculation(value: Any, work: Mapping[str, Any], kv: Mapping[str, Any]) -> dict[str, Any]:
    row = _object(value, "scenario.speculation")
    _exact(
        row,
        {
            "enabled",
            "max_proposed_tokens_per_output",
            "proposed_tokens",
            "verified_tokens",
            "accepted_tokens",
            "rejected_tokens",
            "verifier_dispatches",
        },
        "scenario.speculation",
    )
    enabled = _boolean(row["enabled"], "scenario.speculation.enabled")
    max_proposed = _integer(row["max_proposed_tokens_per_output"], "scenario.speculation.max_proposed_tokens_per_output", maximum=MAX_PROPOSAL_MULTIPLIER)
    counts = {
        field: _integer(row[field], f"scenario.speculation.{field}", maximum=MAX_TOKENS)
        for field in ("proposed_tokens", "verified_tokens", "accepted_tokens", "rejected_tokens", "verifier_dispatches")
    }
    if not enabled:
        if max_proposed != 0 or any(counts.values()):
            raise ContractError("disabled speculation must use zero proposal limits and counters")
        return copy.deepcopy(row)
    if max_proposed < 1:
        raise ContractError("enabled speculation requires max_proposed_tokens_per_output >= 1")
    proposal_capacity = work["total_generated_tokens"] * max_proposed
    if counts["proposed_tokens"] > proposal_capacity:
        raise ContractError("scenario.speculation.proposed_tokens exceeds explicit proposal capacity")
    if counts["verified_tokens"] > counts["proposed_tokens"]:
        raise ContractError("scenario.speculation.verified_tokens exceeds proposed_tokens")
    if counts["accepted_tokens"] + counts["rejected_tokens"] != counts["verified_tokens"]:
        raise ContractError("scenario.speculation accepted_tokens + rejected_tokens must equal verified_tokens")
    if counts["accepted_tokens"] > work["total_generated_tokens"]:
        raise ContractError("scenario.speculation.accepted_tokens exceeds logical output tokens")
    if counts["verifier_dispatches"] > counts["proposed_tokens"]:
        raise ContractError("scenario.speculation.verifier_dispatches exceeds proposed_tokens")
    if kv["rollback_tokens"] > counts["proposed_tokens"] - counts["accepted_tokens"]:
        raise ContractError("scenario.kv.rollback_tokens exceeds speculative unaccepted proposal tokens")
    return copy.deepcopy(row)


def _validate_measurements(value: Any, provenance: Mapping[str, Any]) -> dict[str, Any]:
    row = _object(value, "scenario.measurements")
    _exact(
        row,
        {
            "kind",
            "simulated_elapsed_ms",
            "wall_clock_samples_ms",
            "wall_clock_observations_sha256",
        },
        "scenario.measurements",
    )
    kind = _enum(row["kind"], TIMING_KINDS, "scenario.measurements.kind")
    simulated = _number(
        row["simulated_elapsed_ms"],
        "scenario.measurements.simulated_elapsed_ms",
        minimum=0.000001,
        maximum=MAX_DURATION_MS,
        nullable=True,
    )
    samples = row["wall_clock_samples_ms"]
    if not isinstance(samples, list):
        raise ContractError("scenario.measurements.wall_clock_samples_ms must be a list")
    for index, sample in enumerate(samples):
        _number(sample, f"scenario.measurements.wall_clock_samples_ms[{index}]", minimum=0.000001, maximum=MAX_DURATION_MS)
    evidence = _sha256(
        row["wall_clock_observations_sha256"],
        "scenario.measurements.wall_clock_observations_sha256",
        nullable=True,
    )
    if kind == "analytic_simulation":
        if provenance["trace_kind"] != "analytic_simulation":
            raise ContractError("analytic measurements require analytic_simulation provenance")
        if simulated is None or samples or evidence is not None:
            raise ContractError("analytic measurements require only simulated_elapsed_ms")
    else:
        if provenance["trace_kind"] != "runtime_trace":
            raise ContractError("real wall-clock measurements require runtime_trace provenance")
        if simulated is not None or len(samples) < 3 or evidence is None:
            raise ContractError("real wall-clock measurements require >=3 samples, an evidence SHA, and no simulated time")
    return copy.deepcopy(row)


def _validate_target_ledger(value: Any) -> dict[str, Any]:
    row = _object(value, "scenario.target_ledger")
    _exact(row, {"target_speedup", "factors"}, "scenario.target_ledger")
    target = _number(row["target_speedup"], "scenario.target_ledger.target_speedup", minimum=20.0, maximum=1_000_000.0)
    factors = row["factors"]
    if not isinstance(factors, list) or len(factors) != len(REQUIRED_LEDGER_LANES):
        raise ContractError("scenario.target_ledger.factors must contain each required 20x lane exactly once")
    seen: set[str] = set()
    for index, raw in enumerate(factors):
        factor = _object(raw, f"scenario.target_ledger.factors[{index}]")
        _exact(
            factor,
            {
                "lane",
                "planned_ratio",
                "observed_ratio",
                "evidence_kind",
                "evidence_sha256",
                "correlation_group",
                "combination_evidence_sha256",
            },
            f"scenario.target_ledger.factors[{index}]",
        )
        lane = _enum(factor["lane"], set(REQUIRED_LEDGER_LANES), f"scenario.target_ledger.factors[{index}].lane")
        if lane in seen:
            raise ContractError(f"scenario.target_ledger has duplicate lane {lane}")
        seen.add(lane)
        _number(factor["planned_ratio"], f"scenario.target_ledger.factors[{index}].planned_ratio", minimum=0.000001, maximum=1_000_000.0)
        observed = _number(
            factor["observed_ratio"],
            f"scenario.target_ledger.factors[{index}].observed_ratio",
            minimum=0.000001,
            maximum=1_000_000.0,
            nullable=True,
        )
        evidence_kind = _enum(factor["evidence_kind"], LEDGER_EVIDENCE_KINDS, f"scenario.target_ledger.factors[{index}].evidence_kind")
        evidence = _sha256(factor["evidence_sha256"], f"scenario.target_ledger.factors[{index}].evidence_sha256", nullable=True)
        combination = _sha256(factor["combination_evidence_sha256"], f"scenario.target_ledger.factors[{index}].combination_evidence_sha256", nullable=True)
        _identifier(factor["correlation_group"], f"scenario.target_ledger.factors[{index}].correlation_group")
        if evidence_kind == "unproven":
            if observed is not None or evidence is not None or combination is not None:
                raise ContractError("unproven ledger factors cannot carry observations or evidence hashes")
        elif evidence is None or observed is None:
            raise ContractError("observed ledger factors require observed_ratio and evidence_sha256")
        elif evidence_kind != "real_wall_clock" and combination is not None:
            raise ContractError("only real wall-clock factor evidence may prove a correlated combination")
    if seen != set(REQUIRED_LEDGER_LANES):
        raise ContractError("scenario.target_ledger is missing a required 20x lane")
    return copy.deepcopy(row)


def validate_scenario(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return a deep copy of one bounded scenario manifest."""

    scenario = _object(value, "scenario")
    _exact(
        scenario,
        {
            "schema_version",
            "scenario_id",
            "comparison_id",
            "arm",
            "provenance",
            "logical_work",
            "scheduler",
            "kv",
            "speculation",
            "measurements",
            "target_ledger",
        },
        "scenario",
    )
    if type(scenario["schema_version"]) is not int or scenario["schema_version"] != SCHEMA_VERSION:
        raise ContractError(f"scenario.schema_version must be integer {SCHEMA_VERSION}")
    _identifier(scenario["scenario_id"], "scenario.scenario_id")
    _identifier(scenario["comparison_id"], "scenario.comparison_id")
    _enum(scenario["arm"], {"baseline", "candidate"}, "scenario.arm")
    provenance = _validate_provenance(scenario["provenance"])
    work = _validate_logical_work(scenario["logical_work"])
    scheduler = _validate_scheduler(scenario["scheduler"], work)
    kv = _validate_kv(scenario["kv"], work)
    _validate_speculation(scenario["speculation"], work, kv)
    _validate_measurements(scenario["measurements"], provenance)
    if scenario["arm"] == "baseline":
        if scenario["target_ledger"] is not None:
            raise ContractError("baseline scenario.target_ledger must be null; the candidate owns the 20x plan")
    else:
        _validate_target_ledger(scenario["target_ledger"])
    return copy.deepcopy(scenario)


def compare_scenarios(baseline_value: Mapping[str, Any], candidate_value: Mapping[str, Any]) -> dict[str, Any]:
    """Fail closed unless the two traces describe identical logical work."""

    baseline = validate_scenario(baseline_value)
    candidate = validate_scenario(candidate_value)
    if baseline["arm"] != "baseline" or candidate["arm"] != "candidate":
        raise IncompatibleScenarioError("comparison requires baseline then candidate arm roles")
    if baseline["scenario_id"] == candidate["scenario_id"]:
        raise IncompatibleScenarioError("baseline and candidate scenario_id must differ")
    if baseline["comparison_id"] != candidate["comparison_id"]:
        raise IncompatibleScenarioError("comparison_id differs")
    if baseline["logical_work"] != candidate["logical_work"]:
        raise IncompatibleScenarioError("logical_work differs; refusing an apples-to-oranges speed ratio")
    for field in (
        "model_sha256",
        "tokenizer_sha256",
        "hardware_fingerprint_sha256",
        "workload_source_sha256",
        "timing_boundary",
    ):
        if baseline["provenance"][field] != candidate["provenance"][field]:
            raise IncompatibleScenarioError(f"provenance.{field} differs")
    if baseline["measurements"]["kind"] != candidate["measurements"]["kind"]:
        raise IncompatibleScenarioError("measurement kind differs; do not compare real and analytic time")
    return {
        "comparison_id": baseline["comparison_id"],
        "logical_work_sha256": sha256_json(baseline["logical_work"]),
        "baseline_scenario_sha256": sha256_json(baseline),
        "candidate_scenario_sha256": sha256_json(candidate),
        "timing_kind": baseline["measurements"]["kind"],
        "timing_boundary": baseline["provenance"]["timing_boundary"],
    }


def summarize_scenario(value: Mapping[str, Any]) -> dict[str, Any]:
    """Derive bounded counters and ratios, retaining their scenario provenance."""

    scenario = validate_scenario(value)
    work = scenario["logical_work"]
    scheduler = scenario["scheduler"]
    kv = scenario["kv"]
    spec = scenario["speculation"]
    widths = scheduler["batch_widths"]
    output_tokens = work["total_generated_tokens"]
    fresh_committed = work["total_prompt_tokens"] - kv["prefix_reused_prompt_tokens"] + output_tokens
    total_row_dispatches = sum(widths)
    timing = scenario["measurements"]
    if timing["kind"] == "real_wall_clock":
        samples = [float(sample) for sample in timing["wall_clock_samples_ms"]]
        elapsed_ms = _percentile(samples, 50)
        timing_summary = {
            "kind": "real_wall_clock",
            "sample_count": len(samples),
            "median_elapsed_ms": _rounded(elapsed_ms),
            "p95_elapsed_ms": _rounded(_percentile(samples, 95)),
            "observations_sha256": timing["wall_clock_observations_sha256"],
        }
    else:
        elapsed_ms = float(timing["simulated_elapsed_ms"])
        timing_summary = {
            "kind": "analytic_simulation",
            "sample_count": 0,
            "simulated_elapsed_ms": _rounded(elapsed_ms),
            "observations_sha256": None,
        }
    proposal_capacity = output_tokens * spec["max_proposed_tokens_per_output"]
    scheduler_metrics = {
        "dispatches": scheduler["dispatches"],
        "prefill_dispatches": scheduler["prefill_dispatches"],
        "decode_dispatches": scheduler["decode_dispatches"],
        "compactions": scheduler["compactions"],
        "max_active_slots": scheduler["max_active_slots"],
        "active_row_dispatches": total_row_dispatches,
        "mean_batch_width": _rounded(total_row_dispatches / scheduler["dispatches"]),
        "p95_batch_width": _rounded(_percentile(widths, 95)),
        "slot_utilization": _rounded(total_row_dispatches / (scheduler["dispatches"] * scheduler["max_active_slots"])),
        "dispatches_per_output_token": _rounded(scheduler["dispatches"] / output_tokens),
        "bounds": {
            "minimum_batch_width": 1,
            "maximum_batch_width": scheduler["max_active_slots"],
            "maximum_active_row_dispatches": scheduler["dispatches"] * scheduler["max_active_slots"],
        },
    }
    kv_metrics = {
        "allocated_token_slots": kv["allocated_token_slots"],
        "peak_live_token_slots": kv["peak_live_token_slots"],
        "live_token_slots_after": kv["live_token_slots_after"],
        "prefix_reused_prompt_tokens": kv["prefix_reused_prompt_tokens"],
        "prefix_reuse_fraction": _rounded(kv["prefix_reused_prompt_tokens"] / work["total_prompt_tokens"]) if work["total_prompt_tokens"] else 0.0,
        "capacity_utilization": _rounded(kv["peak_live_token_slots"] / kv["allocated_token_slots"]),
        "fresh_committed_tokens": fresh_committed,
        "kv_write_tokens": kv["kv_write_tokens"],
        "kv_write_amplification": _rounded(kv["kv_write_tokens"] / fresh_committed),
        "rollback_tokens": kv["rollback_tokens"],
        "rollback_fraction_of_writes": _rounded(kv["rollback_tokens"] / kv["kv_write_tokens"]),
        "evictions": kv["evictions"],
        "bounds": {
            "peak_live_token_slots_lte": kv["allocated_token_slots"],
            "prefix_reuse_tokens_lte": work["total_prompt_tokens"],
            "kv_write_tokens_lte": fresh_committed * kv["max_write_amplification"],
        },
    }
    spec_metrics = {
        "enabled": spec["enabled"],
        "proposed_tokens": spec["proposed_tokens"],
        "verified_tokens": spec["verified_tokens"],
        "accepted_tokens": spec["accepted_tokens"],
        "rejected_tokens": spec["rejected_tokens"],
        "verifier_dispatches": spec["verifier_dispatches"],
        "accepted_fraction_of_verified": _rounded(spec["accepted_tokens"] / spec["verified_tokens"]) if spec["verified_tokens"] else 0.0,
        "accepted_fraction_of_output": _rounded(spec["accepted_tokens"] / output_tokens),
        "proposals_per_output_token": _rounded(spec["proposed_tokens"] / output_tokens),
        "verified_tokens_per_verifier_dispatch": _rounded(spec["verified_tokens"] / spec["verifier_dispatches"]) if spec["verifier_dispatches"] else 0.0,
        "bounds": {
            "proposed_tokens_lte": proposal_capacity,
            "accepted_tokens_lte": output_tokens,
            "rollback_tokens_lte_unaccepted_proposals": spec["proposed_tokens"] - spec["accepted_tokens"],
        },
    }
    return {
        "scenario_id": scenario["scenario_id"],
        "arm": scenario["arm"],
        "scenario_sha256": sha256_json(scenario),
        "provenance": {
            "trace_kind": scenario["provenance"]["trace_kind"],
            "trace_sha256": scenario["provenance"]["trace_sha256"],
            "engine_build_sha256": scenario["provenance"]["engine_build_sha256"],
            "hardware_fingerprint_sha256": scenario["provenance"]["hardware_fingerprint_sha256"],
            "logical_work_sha256": sha256_json(work),
            "timing_boundary": scenario["provenance"]["timing_boundary"],
            "measurement_kind": timing["kind"],
        },
        "logical_work": copy.deepcopy(work),
        "timing": timing_summary,
        "throughput_output_tokens_per_second": _rounded(output_tokens * 1000 / elapsed_ms),
        "scheduler": scheduler_metrics,
        "kv": kv_metrics,
        "speculation": spec_metrics,
    }


def build_target_ledger(
    ledger_value: Mapping[str, Any], *, direct_speedup_ratio: float, timing_kind: str
) -> dict[str, Any]:
    """Make an explicit plan ledger without treating factor multiplication as proof."""

    ledger = _validate_target_ledger(ledger_value)
    groups: dict[str, list[dict[str, Any]]] = {}
    for factor in ledger["factors"]:
        groups.setdefault(factor["correlation_group"], []).append(factor)
    conservative_plan_product = 1.0
    all_factors_proven = True
    refusals: list[dict[str, Any]] = []
    group_rows: list[dict[str, Any]] = []
    for group_name in sorted(groups):
        factors = sorted(groups[group_name], key=lambda row: row["lane"])
        planned = [float(row["planned_ratio"]) for row in factors]
        observed = [row["observed_ratio"] for row in factors]
        same_combination_proof = len({row["combination_evidence_sha256"] for row in factors}) == 1
        combination_proof = factors[0]["combination_evidence_sha256"] if same_combination_proof else None
        proven_group = all(row["evidence_kind"] == "real_wall_clock" for row in factors)
        may_multiply_group = len(factors) == 1 or (proven_group and bool(combination_proof))
        if may_multiply_group:
            planned_component = math.prod(planned)
        else:
            # The maximum is not a performance claim.  It is a deliberately
            # conservative planning envelope that refuses to stack correlated wins.
            planned_component = max(planned)
            refusals.append(
                {
                    "correlation_group": group_name,
                    "lanes": [row["lane"] for row in factors],
                    "reason": "unproven_or_uncombined_correlation",
                }
            )
        conservative_plan_product *= planned_component
        if not (may_multiply_group and proven_group and all(item is not None for item in observed)):
            all_factors_proven = False
            observed_component = None
        else:
            observed_component = math.prod(float(item) for item in observed if item is not None)
        group_rows.append(
            {
                "correlation_group": group_name,
                "lanes": [row["lane"] for row in factors],
                "planned_ratio_used": _rounded(planned_component),
                "observed_ratio_used": _rounded(observed_component) if observed_component is not None else None,
                "multiplication_allowed": may_multiply_group,
                "combination_evidence_sha256": combination_proof if may_multiply_group and len(factors) > 1 else None,
            }
        )
    direct_real_20x = timing_kind == "real_wall_clock" and direct_speedup_ratio >= ledger["target_speedup"]
    if direct_real_20x:
        direct_status = "DIRECT_REAL_WALL_CLOCK_TARGET_OBSERVED_NOT_A_PRODUCTION_CLAIM"
    elif timing_kind == "real_wall_clock":
        direct_status = "DIRECT_REAL_WALL_CLOCK_BELOW_TARGET_NOT_A_PRODUCTION_CLAIM"
    else:
        direct_status = "SIMULATED_ONLY_NOT_PROVEN"
    return {
        "target_speedup": _rounded(float(ledger["target_speedup"])),
        "required_lanes": list(REQUIRED_LEDGER_LANES),
        "factors": copy.deepcopy(sorted(ledger["factors"], key=lambda row: row["lane"])),
        "groups": group_rows,
        "factor_product_refused": bool(refusals),
        "refused_correlated_groups": refusals,
        "declared_full_plan_product": None if refusals else _rounded(math.prod(float(row["planned_ratio"]) for row in ledger["factors"])),
        "conservative_noncorrelated_plan_product": _rounded(conservative_plan_product),
        "component_product_proven": all_factors_proven and not refusals,
        "direct_same_work_speedup_ratio": _rounded(direct_speedup_ratio),
        "target_status": direct_status,
        "notice": "Component ratios never prove 20x. Only the direct same-work wall-clock ratio can observe the target; this remains a non-production artifact.",
    }


def evaluate_pair(baseline_value: Mapping[str, Any], candidate_value: Mapping[str, Any]) -> dict[str, Any]:
    """Compare compatible scenarios and return a deterministic planning artifact."""

    comparison = compare_scenarios(baseline_value, candidate_value)
    baseline = summarize_scenario(baseline_value)
    candidate = summarize_scenario(candidate_value)
    baseline_elapsed = (
        baseline["timing"].get("median_elapsed_ms")
        if comparison["timing_kind"] == "real_wall_clock"
        else baseline["timing"]["simulated_elapsed_ms"]
    )
    candidate_elapsed = (
        candidate["timing"].get("median_elapsed_ms")
        if comparison["timing_kind"] == "real_wall_clock"
        else candidate["timing"]["simulated_elapsed_ms"]
    )
    assert isinstance(baseline_elapsed, (float, int)) and isinstance(candidate_elapsed, (float, int))
    speedup_ratio = float(baseline_elapsed) / float(candidate_elapsed)
    candidate_scenario = validate_scenario(candidate_value)
    ledger = build_target_ledger(
        candidate_scenario["target_ledger"],
        direct_speedup_ratio=speedup_ratio,
        timing_kind=comparison["timing_kind"],
    )
    evidence_label = (
        "REAL_WALL_CLOCK_OBSERVATION_INGEST_NOT_A_PRODUCTION_CLAIM"
        if comparison["timing_kind"] == "real_wall_clock"
        else "SIMULATED_ANALYTIC_NOT_A_PRODUCTION_CLAIM"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "resident_engine_20x_benchmark_plan",
        "production_status": "NOT_A_PRODUCTION_CLAIM",
        "evidence_label": evidence_label,
        "comparison": comparison,
        "same_logical_work": True,
        "baseline": baseline,
        "candidate": candidate,
        "direct_speedup_ratio": _rounded(speedup_ratio),
        "direct_target_observed": ledger["target_status"].startswith("DIRECT_REAL_WALL_CLOCK_TARGET_OBSERVED"),
        "target_ledger": ledger,
        "notice": "No model was run by this script. Analytic input remains simulated; wall-clock input is only ingested with its declared provenance.",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True, help="baseline scenario JSON")
    parser.add_argument("--candidate", type=Path, required=True, help="candidate scenario JSON")
    parser.add_argument("--artifact", type=Path, required=True, help="output JSON artifact")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = evaluate_pair(load_json(args.baseline), load_json(args.candidate))
        args.artifact.parent.mkdir(parents=True, exist_ok=True)
        args.artifact.write_bytes(pretty_json_bytes(result))
    except (ContractError, OSError, json.JSONDecodeError) as exc:
        print(f"bench-resident-engine: {exc}", file=sys.stderr)
        return 2
    print(
        "bench-resident-engine: "
        f"evidence={result['evidence_label']} "
        f"speedup={result['direct_speedup_ratio']:.6f}x "
        f"artifact={args.artifact}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
