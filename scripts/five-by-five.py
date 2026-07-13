#!/usr/bin/env python3
"""Validate, report, and optionally execute the canonical 5/5 proof registry."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Any

from source_fingerprint import FingerprintError, source_fingerprint


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "proof" / "5x5-gates.json"
ALLOWED_STATES = {
    "planned",
    "in_progress",
    "ready",
    "proven",
    "external_pending",
    "blocked",
}
ALLOWED_ROLES = {"outcome", "prerequisite"}


def atomic_json_write(path: pathlib.Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def load_registry(path: pathlib.Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if data.get("schema_version") != 1:
        raise ValueError("unsupported schema_version (want 1)")
    facets = data.get("facets")
    if not isinstance(facets, list) or not facets:
        raise ValueError("facets must be a non-empty list")

    facet_ids: set[str] = set()
    gate_ids: set[str] = set()
    for facet in facets:
        facet_id = facet.get("id")
        if not isinstance(facet_id, str) or not facet_id:
            raise ValueError("every facet needs a non-empty id")
        if facet_id in facet_ids:
            raise ValueError(f"duplicate facet id: {facet_id}")
        facet_ids.add(facet_id)
        if facet.get("target") != 5:
            raise ValueError(f"{facet_id}: target must be 5")
        if not facet.get("definition_of_5"):
            raise ValueError(f"{facet_id}: definition_of_5 is required")
        gates = facet.get("gates")
        if not isinstance(gates, list) or not gates:
            raise ValueError(f"{facet_id}: gates must be a non-empty list")
        for gate in gates:
            gate_id = gate.get("id")
            qualified = f"{facet_id}/{gate_id}"
            if not isinstance(gate_id, str) or not gate_id:
                raise ValueError(f"{facet_id}: every gate needs a non-empty id")
            if qualified in gate_ids:
                raise ValueError(f"duplicate gate id: {qualified}")
            gate_ids.add(qualified)
            if gate.get("state") not in ALLOWED_STATES:
                raise ValueError(f"{qualified}: invalid state {gate.get('state')!r}")
            if gate.get("role", "outcome") not in ALLOWED_ROLES:
                raise ValueError(f"{qualified}: invalid role {gate.get('role')!r}")
            for required in ("scope", "acceptance", "owner"):
                if not gate.get(required):
                    raise ValueError(f"{qualified}: {required} is required")
            command = gate.get("command")
            evidence_validator = gate.get("evidence_validator")
            if command and evidence_validator:
                raise ValueError(
                    f"{qualified}: choose command or evidence_validator, not both"
                )
            if gate.get("state") == "proven" and not (command or evidence_validator):
                raise ValueError(
                    f"{qualified}: a proven gate needs a repeatable command or evidence_validator"
                )
            if gate.get("state") != "proven" and not gate.get("next_action"):
                raise ValueError(
                    f"{qualified}: every unproven gate needs a concrete next_action"
                )
    return data


def select_facets(data: dict[str, Any], wanted: list[str]) -> list[dict[str, Any]]:
    facets = data["facets"]
    if not wanted:
        return facets
    by_id = {facet["id"]: facet for facet in facets}
    unknown = sorted(set(wanted) - by_id.keys())
    if unknown:
        raise ValueError("unknown facet(s): " + ", ".join(unknown))
    return [by_id[item] for item in wanted]


def print_report(facets: list[dict[str, Any]]) -> None:
    for facet in facets:
        gates = facet["gates"]
        outcomes = [gate for gate in gates if gate.get("role", "outcome") == "outcome"]
        prerequisites = [gate for gate in gates if gate.get("role") == "prerequisite"]
        outcomes_proven = sum(gate["state"] == "proven" for gate in outcomes)
        prerequisites_proven = sum(gate["state"] == "proven" for gate in prerequisites)
        facet_complete = all(gate["state"] == "proven" for gate in gates)
        runnable = sum(bool(gate.get("command") or gate.get("evidence_validator")) for gate in gates)
        external = sum(gate["state"] == "external_pending" for gate in gates)
        print(
            f"{facet['id']}: 5/5 {'YES' if facet_complete else 'NO'} | "
            f"outcomes proven {outcomes_proven}/{len(outcomes)} | "
            f"prerequisites proven {prerequisites_proven}/{len(prerequisites)} | "
            f"runnable {runnable} | external pending {external}"
        )
        for gate in gates:
            marker = {
                "proven": "PROVEN",
                "ready": "READY",
                "in_progress": "WORK",
                "external_pending": "EXT",
                "blocked": "BLOCK",
                "planned": "PLAN",
            }[gate["state"]]
            role = " (prerequisite)" if gate.get("role") == "prerequisite" else ""
            print(f"  [{marker:7}] {gate['id']}{role}: {gate['acceptance']}")
            if gate["state"] != "proven":
                print(f"             -> {gate['next_action']}")


def run_commands(
    facets: list[dict[str, Any]], artifact_dir: pathlib.Path, registry_path: pathlib.Path
) -> int:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = artifact_dir / "ledger.jsonl"
    metadata_path = artifact_dir / "source.json"
    partial_path = artifact_dir / "source.partial.json"
    lock_handle = (artifact_dir / ".run.lock").open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"5x5 artifact directory is already in use: {artifact_dir}", file=sys.stderr)
        lock_handle.close()
        return 2
    metadata_path.unlink(missing_ok=True)
    partial_path.unlink(missing_ok=True)
    try:
        source_start = source_fingerprint(ROOT)
    except FingerprintError as exc:
        print(f"5x5 source fingerprint error: {exc}", file=sys.stderr)
        lock_handle.close()
        return 2
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "evidence_scope": "attached_commands_only_not_facet_5x5",
        "run_status": "RUNNING",
        "started_at_unix": int(time.time()),
        "registry": str(registry_path),
        "registry_sha256": hashlib.sha256(registry_path.read_bytes()).hexdigest(),
        "facets": [facet["id"] for facet in facets],
        "selected_gate_count": sum(len(facet["gates"]) for facet in facets),
        "facet_5x5_at_start": all(
            gate["state"] == "proven" for facet in facets for gate in facet["gates"]
        ),
        "unexecuted_gates": [
            f"{facet['id']}/{gate['id']}"
            for facet in facets
            for gate in facet["gates"]
            if not (gate.get("command") or gate.get("evidence_validator"))
        ],
        "source_start": source_start,
    }
    atomic_json_write(partial_path, metadata)
    failures = 0
    commands_run = 0
    # A named artifact directory represents one run, never an append-only mixture
    # of old and new source snapshots.
    with ledger_path.open("w", encoding="utf-8") as ledger:
        for facet in facets:
            for gate in facet["gates"]:
                command = gate.get("command") or gate.get("evidence_validator")
                if not command:
                    continue
                commands_run += 1
                execution_kind = "command" if gate.get("command") else "evidence_validator"
                started = time.time()
                print(f"RUN {facet['id']}/{gate['id']}: {command}", flush=True)
                completed = subprocess.run(
                    command,
                    cwd=ROOT,
                    shell=True,
                    executable="/bin/zsh",
                    text=True,
                    capture_output=True,
                )
                record = {
                    "facet": facet["id"],
                    "gate": gate["id"],
                    "command": command,
                    "execution_kind": execution_kind,
                    "head": source_start["head"],
                    "source_sha256": source_start["source_sha256"],
                    "exit_code": completed.returncode,
                    "duration_ms": round((time.time() - started) * 1000),
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                }
                ledger.write(json.dumps(record, sort_keys=True) + "\n")
                ledger.flush()
                status = "PASS" if completed.returncode == 0 else "FAIL"
                print(f"{status} {facet['id']}/{gate['id']}")
                if completed.returncode != 0:
                    failures += 1
                    if completed.stderr:
                        print(completed.stderr.rstrip(), file=sys.stderr)
    try:
        source_end = source_fingerprint(ROOT)
    except FingerprintError as exc:
        print(f"5x5 final source fingerprint error: {exc}", file=sys.stderr)
        lock_handle.close()
        return 2
    stable = (
        source_start["source_sha256"] == source_end["source_sha256"]
        and source_start["status_sha256"] == source_end["status_sha256"]
    )
    command_failures = failures
    if commands_run == 0:
        failures += 1
        print("FAIL no attached gate commands were executed", file=sys.stderr)
    if not stable:
        failures += 1
        print("FAIL source changed while attached gate commands were running", file=sys.stderr)
    metadata["finished_at_unix"] = int(time.time())
    metadata["source_end"] = source_end
    metadata["source_stable"] = stable
    metadata["commands_run"] = commands_run
    metadata["command_failures"] = command_failures
    metadata["total_failures"] = failures
    metadata["run_status"] = "PASS" if failures == 0 else "FAIL"
    metadata["ledger_sha256"] = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    atomic_json_write(metadata_path, metadata)
    partial_path.unlink(missing_ok=True)
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    lock_handle.close()
    print(f"ledger: {ledger_path}")
    print(f"source: {metadata_path}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=pathlib.Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--facet", action="append", default=[], help="facet id (repeatable)")
    parser.add_argument("--run", action="store_true", help="run commands attached to selected gates")
    parser.add_argument(
        "--artifact-dir",
        type=pathlib.Path,
        default=ROOT / ".artifacts" / "5x5" / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
    )
    args = parser.parse_args()
    try:
        data = load_registry(args.registry.resolve())
        facets = select_facets(data, args.facet)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"5x5 registry error: {exc}", file=sys.stderr)
        return 2
    print_report(facets)
    if args.run:
        return run_commands(facets, args.artifact_dir.resolve(), args.registry.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
