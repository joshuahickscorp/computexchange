#!/usr/bin/env python3
"""Prepare, but never execute, a pinned shared-prefix vLLM ABBA workload.

The helper turns an operator-supplied, pre-tokenized source contract into
immutable artifacts for a later shared-prefix-reuse measurement lane:

* a workload whose every request has the same declared input-token prefix and
  a distinct, non-empty fresh suffix;
* separate direct/no-prefix baseline and native-prefix-cache candidate launch
  plans; and
* arm-specific resolved-config inputs plus runtime identities that preserve a
  common engine/model/runtime identity.

It never imports vLLM, starts a process, contacts an endpoint, warms a cache,
or measures time.  It explicitly forbids response reuse and speculation in
this lane.  A future runner must still obtain a physical endpoint attestation
and a per-call native-prefix telemetry trace before it can record cache hits.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence
import urllib.parse


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import cx_inference_policy_v1 as policy  # noqa: E402
import prepare_vllm_exact_cache_workload_v1 as exact_prep  # noqa: E402
import screen_inference_lane_abba as abba  # noqa: E402


SCHEMA_VERSION = 1
SOURCE_KIND = "cx_vllm_shared_prefix_prep_source_v1"
WORKLOAD_KIND = "cx_inference_shared_prefix_workload_v1"
LAUNCH_PLAN_KIND = "cx_vllm_shared_prefix_launch_plan_v1"
RUNTIME_INPUT_KIND = "cx_vllm_shared_prefix_runtime_identity_input_v1"
MAX_REQUESTS = 4_096
MAX_OUTPUT_TOKENS = 32_768
IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,127}$")

# Reuse the already-tested file, tree, and canonical-JSON safety primitives.
# The exact-cache semantic parser is intentionally not reused: this module has
# its own lane schema and does not authorize response-cache reuse.
PreparationError = exact_prep.PreparationError
canonical_json_bytes = exact_prep.canonical_json_bytes
sha256_bytes = exact_prep.sha256_bytes
sha256_json = exact_prep.sha256_json


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        raise PreparationError(f"{label}_must_be_lowercase_identifier")
    return value


def _stable(value: Any) -> Any:
    return json.loads(canonical_json_bytes(value).decode("utf-8"))


def _flag_present(argv: Sequence[str], flag: str) -> bool:
    return any(item == flag or item.startswith(f"{flag}=") for item in argv)


def _parse_launch(
    value: Any,
    *,
    arm: str,
    model: Mapping[str, str],
    endpoint: Mapping[str, Any],
    max_total_tokens: int,
    concurrency: int,
) -> dict[str, Any]:
    raw = exact_prep._exact(value, {"argv", "environment"}, f"launches.{arm}")
    argv_raw = raw["argv"]
    if not isinstance(argv_raw, list) or len(argv_raw) < 3:
        raise PreparationError(f"launches.{arm}_argv_invalid")
    argv = [exact_prep._string(item, f"launches.{arm}.argv_{index}", maximum=8192) for index, item in enumerate(argv_raw)]
    if not Path(argv[0]).is_absolute() or argv[1] != "serve":
        raise PreparationError(f"launches.{arm}_must_be_absolute_vllm_serve_command")
    if _flag_present(argv, "--speculative-config"):
        raise PreparationError(f"launches.{arm}_speculative_decode_forbidden")
    if _flag_present(argv, "--async-scheduling") or not _flag_present(argv, "--no-async-scheduling"):
        raise PreparationError(f"launches.{arm}_must_disable_async_scheduling")
    prefix_enabled = _flag_present(argv, "--enable-prefix-caching")
    prefix_disabled = _flag_present(argv, "--no-enable-prefix-caching")
    if arm == "baseline":
        if prefix_enabled or not prefix_disabled:
            raise PreparationError("baseline_launch_must_disable_prefix_cache")
    elif arm == "candidate":
        if not prefix_enabled or prefix_disabled:
            raise PreparationError("candidate_launch_must_enable_prefix_cache")
    else:  # pragma: no cover - internal caller only.
        raise PreparationError("launch_arm_invalid")
    if exact_prep._flag_value(argv, "--served-model-name") != model["model_id"]:
        raise PreparationError(f"launches.{arm}_served_model_name_mismatch")
    revision = exact_prep._flag_value(argv, "--revision")
    if revision is not None and revision != model["model_revision"]:
        raise PreparationError(f"launches.{arm}_revision_mismatch")
    parsed = urllib.parse.urlsplit(str(endpoint["url"]))
    port = str(parsed.port or (443 if parsed.scheme == "https" else 80))
    if exact_prep._flag_value(argv, "--host") != parsed.hostname or exact_prep._flag_value(argv, "--port") != port:
        raise PreparationError(f"launches.{arm}_endpoint_host_or_port_mismatch")
    for flag, required in (
        ("--max-model-len", max_total_tokens),
        ("--max-num-batched-tokens", max_total_tokens),
        ("--max-num-seqs", concurrency),
    ):
        raw_value = exact_prep._flag_value(argv, flag)
        try:
            actual = int(raw_value) if raw_value is not None else 0
        except ValueError as exc:
            raise PreparationError(f"launches.{arm}_{flag.removeprefix('--')}_invalid") from exc
        if actual < required:
            raise PreparationError(f"launches.{arm}_{flag.removeprefix('--')}_too_small")
    environment_raw = raw["environment"]
    if not isinstance(environment_raw, dict):
        raise PreparationError(f"launches.{arm}_environment_must_be_object")
    environment: dict[str, str] = {}
    for name, setting in environment_raw.items():
        if not isinstance(name, str) or not exact_prep.ENV_RE.fullmatch(name) or not isinstance(setting, str):
            raise PreparationError(f"launches.{arm}_environment_invalid")
        if any(word in name for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "AUTH")):
            raise PreparationError(f"launches.{arm}_environment_must_not_retain_secrets")
        environment[name] = setting
    if environment.get("VLLM_METAL_USE_PAGED_ATTENTION") != "1" or environment.get("VLLM_ENABLE_V1_MULTIPROCESSING") != "0":
        raise PreparationError(f"launches.{arm}_metal_required_environment_missing")
    return {"argv": argv, "environment": dict(sorted(environment.items()))}


def _comparable_argv(argv: Sequence[str]) -> list[str]:
    """Drop the only per-arm launch fields permitted by this lane.

    Separate endpoints need different ports and their cache switches must
    disagree by construction.  Every other argv byte is comparison-critical:
    allowing a different scheduler, dtype, memory fraction, or model option
    would turn a prefix-cache experiment into a compound configuration test.
    """

    normalized: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item in {"--host", "--port"}:
            index += 2
            continue
        if item in {"--enable-prefix-caching", "--no-enable-prefix-caching"} or item.startswith(
            "--enable-prefix-caching="
        ) or item.startswith("--no-enable-prefix-caching="):
            index += 1
            continue
        normalized.append(item)
        index += 1
    return normalized


def _require_comparable_arms(
    endpoints: Mapping[str, Mapping[str, Any]], launches: Mapping[str, Mapping[str, Any]]
) -> None:
    """Require the two physical server plans to differ only by cache/port."""

    baseline_endpoint = urllib.parse.urlsplit(str(endpoints["baseline"]["url"]))
    candidate_endpoint = urllib.parse.urlsplit(str(endpoints["candidate"]["url"]))
    baseline_shape = (
        baseline_endpoint.scheme,
        baseline_endpoint.hostname,
        baseline_endpoint.path,
        endpoints["baseline"]["timeout_secs"],
        endpoints["baseline"]["authorization_env"],
    )
    candidate_shape = (
        candidate_endpoint.scheme,
        candidate_endpoint.hostname,
        candidate_endpoint.path,
        endpoints["candidate"]["timeout_secs"],
        endpoints["candidate"]["authorization_env"],
    )
    if baseline_shape != candidate_shape:
        raise PreparationError("baseline_and_candidate_endpoints_must_differ_only_by_port")
    if launches["baseline"]["environment"] != launches["candidate"]["environment"]:
        raise PreparationError("baseline_and_candidate_environment_mismatch")
    if _comparable_argv(launches["baseline"]["argv"]) != _comparable_argv(
        launches["candidate"]["argv"]
    ):
        raise PreparationError("baseline_and_candidate_launches_must_differ_only_by_prefix_cache_and_port")


def _parse_source(value: Mapping[str, Any]) -> dict[str, Any]:
    raw = exact_prep._exact(
        value,
        {"schema_version", "kind", "model", "endpoints", "workload", "launches"},
        "source",
    )
    if raw["schema_version"] != SCHEMA_VERSION or raw["kind"] != SOURCE_KIND:
        raise PreparationError("source_identity_invalid")
    model = exact_prep._parse_model(raw["model"])
    endpoints_raw = exact_prep._exact(raw["endpoints"], {"baseline", "candidate"}, "source.endpoints")
    endpoints = {
        arm: exact_prep._parse_endpoint(endpoints_raw[arm]) for arm in ("baseline", "candidate")
    }
    workload_raw = exact_prep._exact(
        raw["workload"],
        {"tenant_scope_sha256", "sampling", "max_output_tokens", "concurrency", "shared_prefix", "requests"},
        "source.workload",
    )
    tenant_scope = exact_prep._sha(workload_raw["tenant_scope_sha256"], "source.workload.tenant_scope_sha256")
    sampling_raw = exact_prep._exact(
        workload_raw["sampling"],
        {"temperature", "top_p", "seed", "n"},
        "source.workload.sampling",
    )
    if sampling_raw["temperature"] != 0 or sampling_raw["top_p"] != 1 or sampling_raw["n"] != 1:
        raise PreparationError("source_workload_must_be_greedy_single_completion")
    sampling = {
        "temperature": 0,
        "top_p": 1,
        "seed": exact_prep._integer(sampling_raw["seed"], "source.workload.sampling.seed"),
        "n": 1,
    }
    max_output_tokens = exact_prep._integer(
        workload_raw["max_output_tokens"],
        "source.workload.max_output_tokens",
        minimum=1,
        maximum=MAX_OUTPUT_TOKENS,
    )
    concurrency = exact_prep._integer(
        workload_raw["concurrency"],
        "source.workload.concurrency",
        minimum=1,
        maximum=MAX_REQUESTS,
    )
    prefix_raw = exact_prep._exact(
        workload_raw["shared_prefix"], {"prompt", "token_ids"}, "source.workload.shared_prefix"
    )
    prefix_prompt = exact_prep._string(
        prefix_raw["prompt"], "source.workload.shared_prefix.prompt", maximum=1 << 20
    )
    prefix_tokens = exact_prep._tokens(prefix_raw["token_ids"], "source.workload.shared_prefix.token_ids")
    requests_raw = workload_raw["requests"]
    if not isinstance(requests_raw, list) or not 1 <= len(requests_raw) <= MAX_REQUESTS:
        raise PreparationError("source_workload_requests_invalid")
    requests: list[dict[str, Any]] = []
    suffix_identities: set[str] = set()
    complete_input_identities: set[str] = set()
    suffix_prompts: set[str] = set()
    for index, item in enumerate(requests_raw):
        row = exact_prep._exact(
            item,
            {
                "prompt_suffix",
                "suffix_token_ids",
                "input_token_ids",
                "response_reuse_authorized",
                "prefix_reuse_authorized",
            },
            f"source.workload.requests_{index}",
        )
        if row["response_reuse_authorized"] is not False:
            raise PreparationError("source_shared_prefix_response_reuse_forbidden")
        if row["prefix_reuse_authorized"] is not True:
            raise PreparationError("source_shared_prefix_prefix_reuse_required")
        suffix_prompt = exact_prep._string(
            row["prompt_suffix"], f"source.workload.requests_{index}.prompt_suffix", maximum=1 << 20
        )
        suffix_tokens = exact_prep._tokens(
            row["suffix_token_ids"], f"source.workload.requests_{index}.suffix_token_ids"
        )
        input_tokens = exact_prep._tokens(
            row["input_token_ids"], f"source.workload.requests_{index}.input_token_ids"
        )
        if input_tokens[: len(prefix_tokens)] != prefix_tokens or input_tokens[len(prefix_tokens) :] != suffix_tokens:
            raise PreparationError("source_request_input_tokens_must_be_exact_prefix_plus_suffix")
        suffix_identity = sha256_json({"prompt_suffix": suffix_prompt, "suffix_token_ids": suffix_tokens})
        input_identity = sha256_json(input_tokens)
        if suffix_identity in suffix_identities or input_identity in complete_input_identities or suffix_prompt in suffix_prompts:
            raise PreparationError("source_shared_prefix_suffixes_must_be_distinct")
        suffix_identities.add(suffix_identity)
        complete_input_identities.add(input_identity)
        suffix_prompts.add(suffix_prompt)
        requests.append(
            {
                "prompt_suffix": suffix_prompt,
                "suffix_token_ids": suffix_tokens,
                "input_token_ids": input_tokens,
                "fresh_suffix_sha256": suffix_identity,
            }
        )
    max_total_tokens = max(len(row["input_token_ids"]) + max_output_tokens for row in requests)
    launches_raw = exact_prep._exact(raw["launches"], {"baseline", "candidate"}, "source.launches")
    launches = {
        arm: _parse_launch(
            launches_raw[arm],
            arm=arm,
            model=model,
            endpoint=endpoints[arm],
            max_total_tokens=max_total_tokens,
            concurrency=concurrency,
        )
        for arm in ("baseline", "candidate")
    }
    _require_comparable_arms(endpoints, launches)
    return {
        "model": model,
        "endpoints": endpoints,
        "workload": {
            "tenant_scope_sha256": tenant_scope,
            "sampling": sampling,
            "max_output_tokens": max_output_tokens,
            "concurrency": concurrency,
            "shared_prefix": {"prompt": prefix_prompt, "token_ids": prefix_tokens},
            "requests": requests,
        },
        "launches": launches,
    }


def shared_prefix_request_digest(value: Mapping[str, Any]) -> str:
    """Digest the complete fresh-suffix request envelope used by this lane."""

    return sha256_json(
        {
            "request": value["request"],
            "input_token_ids": value["input_token_ids"],
            "suffix_token_ids": value["suffix_token_ids"],
            "fresh_suffix_sha256": value["fresh_suffix_sha256"],
            "completion_request": value["completion_request"],
        }
    )


def _build_workload(source: Mapping[str, Any], runtime_sha256: str) -> dict[str, Any]:
    model = source["model"]
    work = source["workload"]
    prefix = work["shared_prefix"]
    prefix_digest = sha256_json(prefix["token_ids"])
    requests: list[dict[str, Any]] = []
    for index, row in enumerate(work["requests"]):
        prompt = str(prefix["prompt"]) + str(row["prompt_suffix"])
        completion_request = {
            "model": model["model_id"],
            "prompt": prompt,
            "max_tokens": work["max_output_tokens"],
            **work["sampling"],
            "return_token_ids": True,
            "stream": False,
        }
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
            "shared_prefix_token_ids_sha256": prefix_digest,
            "shared_prefix_token_count": len(prefix["token_ids"]),
            "max_output_tokens": work["max_output_tokens"],
            "concurrency": work["concurrency"],
            "intent": policy.INTENT_EXPERIMENTAL,
            "response_reuse_authorized": False,
            "prefix_reuse_authorized": True,
        }
        request["request_identity_sha256"] = policy.request_identity_sha256(request)
        requests.append(
            {
                "request_index": index,
                "request": request,
                "input_token_ids": row["input_token_ids"],
                "suffix_token_ids": row["suffix_token_ids"],
                "fresh_suffix_sha256": row["fresh_suffix_sha256"],
                "completion_request": completion_request,
            }
        )
    request_digests = [shared_prefix_request_digest(row) for row in requests]
    logical_work = {
        "model": {
            "model_id": model["model_id"],
            "model_revision": model["model_revision"],
            "tokenizer_id": model["tokenizer_id"],
            "tokenizer_revision": model["tokenizer_revision"],
        },
        "corpus_sha256": sha256_json(
            [
                {
                    "request": row["request"],
                    "input_token_ids": row["input_token_ids"],
                    "suffix_token_ids": row["suffix_token_ids"],
                    "fresh_suffix_sha256": row["fresh_suffix_sha256"],
                    "completion_request": row["completion_request"],
                }
                for row in requests
            ]
        ),
        "request_digests": request_digests,
        "request_order_sha256": sha256_json(request_digests),
        "input_token_ids_sha256": sha256_json([row["input_token_ids"] for row in requests]),
        "sampling_contract_sha256": sha256_json(work["sampling"]),
        "sampling": work["sampling"],
        "max_output_tokens": work["max_output_tokens"],
        "concurrency": work["concurrency"],
        "reuse_contract": {
            "eligible_request_indexes": list(range(len(requests))),
            "exact_request_key_schema_sha256": None,
            "shared_prefix_token_ids_sha256": prefix_digest,
            "shared_prefix_token_count": len(prefix["token_ids"]),
            "required_eligible_hit_rate": 1.0,
        },
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": WORKLOAD_KIND,
        "logical_work": logical_work,
        "endpoints": _stable(source["endpoints"]),
        "shared_prefix": {
            "prompt": prefix["prompt"],
            "token_ids": prefix["token_ids"],
            "token_ids_sha256": prefix_digest,
            "token_count": len(prefix["token_ids"]),
        },
        "requests": requests,
    }


def _launch_plan(
    *,
    source_sha256: str,
    source: Mapping[str, Any],
    arm: str,
) -> dict[str, Any]:
    prefix_enabled = arm == "candidate"
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": LAUNCH_PLAN_KIND,
        "source_sha256": source_sha256,
        "arm": arm,
        "endpoint": source["endpoints"][arm],
        "model": {
            key: source["model"][key]
            for key in ("model_id", "model_revision", "precision_id")
        },
        "launch": source["launches"][arm],
        "lane": {
            "name": "shared_prefix_reuse",
            "baseline": "target_only_direct_decode",
            "candidate": "cx_native_prefix_cache_fresh_suffix",
            "prefix_cache_enabled": prefix_enabled,
            "response_cache_enabled": False,
            "speculative_decode_enabled": False,
            "stream": False,
            "return_token_ids": True,
        },
    }


def _runtime_values(
    *,
    source_sha256: str,
    source: Mapping[str, Any],
    host: Mapping[str, Any],
    engine: Mapping[str, Any],
    core: Mapping[str, Any],
    metal: Mapping[str, Any],
    launch_plans: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    runtime_identity = {
        "contract": "cx-vllm-shared-prefix-runtime-identity-v1",
        "source_sha256": source_sha256,
        "host": host,
        "engine": engine,
        "vllm_core": core,
        "metal_runtime": metal,
        "model": source["model"],
    }
    runtime_sha256 = sha256_json(runtime_identity)
    runtime_inputs: dict[str, dict[str, Any]] = {}
    runtimes: dict[str, dict[str, Any]] = {}
    for arm in ("baseline", "candidate"):
        resolved_engine_config_sha256 = sha256_json(launch_plans[arm])
        runtime_inputs[arm] = {
            "schema_version": SCHEMA_VERSION,
            "kind": RUNTIME_INPUT_KIND,
            "arm": arm,
            "source_sha256": source_sha256,
            "runtime_identity_sha256": runtime_sha256,
            "host": host,
            "engine": engine,
            "vllm_core": core,
            "metal_runtime": metal,
            "resolved_engine_config_sha256": resolved_engine_config_sha256,
            "model": source["model"],
        }
        runtimes[arm] = {
            "backend": "metal",
            "host_hardware_sha256": sha256_json(host),
            "core_sha256": core["tree_sha256"],
            "runtime_sha256": runtime_sha256,
            "resolved_engine_config_sha256": resolved_engine_config_sha256,
            "engine_id": "vllm-metal-cx",
            "engine_commit": engine["engine_commit"],
            "metal_runtime_sha256": metal["tree_sha256"],
            "weights_sha256": source["model"]["weights_sha256"],
            "tokenizer_sha256": source["model"]["tokenizer_sha256"],
            "precision_id": source["model"]["precision_id"],
        }
        try:
            abba._parse_runtime(runtimes[arm])
        except abba.InferenceLaneContractError as exc:  # pragma: no cover - schema lock.
            raise PreparationError(f"generated_{arm}_runtime_invalid") from exc
    return runtime_inputs["baseline"], runtime_inputs["candidate"], runtimes["baseline"], runtimes["candidate"]


def prepare(
    *,
    source_path: Path,
    engine_root: Path,
    vllm_core_root: Path,
    metal_runtime_root: Path,
    workload_out: Path,
    baseline_launch_plan_out: Path,
    candidate_launch_plan_out: Path,
    baseline_runtime_input_out: Path,
    candidate_runtime_input_out: Path,
    baseline_runtime_out: Path,
    candidate_runtime_out: Path,
    host: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    outputs = [
        workload_out,
        baseline_launch_plan_out,
        candidate_launch_plan_out,
        baseline_runtime_input_out,
        candidate_runtime_input_out,
        baseline_runtime_out,
        candidate_runtime_out,
    ]
    if len(set(outputs)) != len(outputs):
        raise PreparationError("output_paths_must_differ")
    labels = (
        "workload_out",
        "baseline_launch_plan_out",
        "candidate_launch_plan_out",
        "baseline_runtime_input_out",
        "candidate_runtime_input_out",
        "baseline_runtime_out",
        "candidate_runtime_out",
    )
    paths = [exact_prep._new_absolute_path(path, label) for path, label in zip(outputs, labels, strict=True)]
    source = _parse_source(exact_prep._read_source(source_path))
    source_sha = sha256_json(source)
    host_value = dict(host) if host is not None else exact_prep.capture_host()
    if host_value.get("system") != "Darwin" or host_value.get("machine") != "arm64":
        raise PreparationError("target_must_be_apple_silicon_macos")
    engine = exact_prep._digest_tree(engine_root, "engine")
    engine["engine_commit"] = exact_prep._git_commit(engine_root.resolve(strict=True))
    core = exact_prep._digest_tree(vllm_core_root, "vllm_core")
    metal = exact_prep._digest_tree(metal_runtime_root, "metal_runtime")
    baseline_plan = _launch_plan(source_sha256=source_sha, source=source, arm="baseline")
    candidate_plan = _launch_plan(source_sha256=source_sha, source=source, arm="candidate")
    baseline_input, candidate_input, baseline_runtime, candidate_runtime = _runtime_values(
        source_sha256=source_sha,
        source=source,
        host=host_value,
        engine=engine,
        core=core,
        metal=metal,
        launch_plans={"baseline": baseline_plan, "candidate": candidate_plan},
    )
    workload = _build_workload(source, baseline_runtime["runtime_sha256"])
    try:
        logical = abba._parse_logical_work(workload["logical_work"], "shared_prefix_reuse")
        for row in workload["requests"]:
            policy._validate_request(row["request"])
        if logical.value["reuse_contract"]["shared_prefix_token_count"] != workload["shared_prefix"]["token_count"]:
            raise PreparationError("generated_prefix_count_mismatch")
    except (abba.InferenceLaneContractError, policy.PolicyInputError) as exc:  # pragma: no cover - schema lock.
        raise PreparationError("generated_shared_prefix_workload_invalid") from exc
    values = (
        workload,
        baseline_plan,
        candidate_plan,
        baseline_input,
        candidate_input,
        baseline_runtime,
        candidate_runtime,
    )
    for path, value, label in zip(paths, values, labels, strict=True):
        exact_prep._write_new(path, value, label)
    return {
        "status": "prepared_unmeasured",
        "workload": str(workload_out),
        "baseline_launch_plan": str(baseline_launch_plan_out),
        "candidate_launch_plan": str(candidate_launch_plan_out),
        "baseline_runtime_input": str(baseline_runtime_input_out),
        "candidate_runtime_input": str(candidate_runtime_input_out),
        "baseline_runtime": str(baseline_runtime_out),
        "candidate_runtime": str(candidate_runtime_out),
        "runtime_sha256": baseline_runtime["runtime_sha256"],
        "claim": "CPU-only preparation; no vLLM server, target request, cache warm, ABBA trial, speed, or authorization claim",
    }


def status() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "cx_vllm_shared_prefix_prep_status_v1",
        "measurement_status": "unmeasured",
        "lane": "shared_prefix_reuse",
        "required_source": [
            "pre-tokenized shared prefix",
            "distinct non-empty suffixes",
            "full input tokens equal prefix plus suffix",
            "response_reuse_authorized=false",
            "prefix_reuse_authorized=true",
        ],
        "required_launch": [
            "baseline --no-enable-prefix-caching",
            "candidate --enable-prefix-caching",
            "no --speculative-config",
            "--no-async-scheduling",
        ],
        "claim": "no server started and no performance claim",
    }


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
    command.add_argument("--baseline-launch-plan-out", type=Path, required=True)
    command.add_argument("--candidate-launch-plan-out", type=Path, required=True)
    command.add_argument("--baseline-runtime-input-out", type=Path, required=True)
    command.add_argument("--candidate-runtime-input-out", type=Path, required=True)
    command.add_argument("--baseline-runtime-out", type=Path, required=True)
    command.add_argument("--candidate-runtime-out", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "status":
            result = status()
        else:
            result = prepare(
                source_path=args.source,
                engine_root=args.engine_root,
                vllm_core_root=args.vllm_core_root,
                metal_runtime_root=args.metal_runtime_root,
                workload_out=args.workload_out,
                baseline_launch_plan_out=args.baseline_launch_plan_out,
                candidate_launch_plan_out=args.candidate_launch_plan_out,
                baseline_runtime_input_out=args.baseline_runtime_input_out,
                candidate_runtime_input_out=args.candidate_runtime_input_out,
                baseline_runtime_out=args.baseline_runtime_out,
                candidate_runtime_out=args.candidate_runtime_out,
            )
    except PreparationError as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
