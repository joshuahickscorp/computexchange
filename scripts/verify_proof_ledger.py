#!/usr/bin/env python3
"""Validate a terminal, source-bound prove-local ledger.

This validates evidence already produced by scripts/prove-local.sh. It never runs
the proof itself and cannot upgrade contract-only evidence into a live-agent claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from source_fingerprint import FingerprintError, source_fingerprint


class LedgerError(ValueError):
    """The proof ledger is partial, stale, malformed, or missing required evidence."""


def parse_ledger(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LedgerError(f"cannot read ledger: {exc}") from exc
    meta: dict[str, str] = {}
    passes: dict[str, list[str]] = {}
    skips: dict[str, list[str]] = {}
    failures: dict[str, list[str]] = {}
    for number, raw_line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not raw_line:
            continue
        parts = raw_line.split("\t", 2)
        if len(parts) != 3:
            raise LedgerError(f"line {number} is not a three-column ledger row")
        status, key, detail = parts
        if status == "META":
            if key in meta:
                raise LedgerError(f"duplicate META key: {key}")
            meta[key] = detail
        elif status in {"PASS", "SKIP", "FAIL"}:
            bucket = {"PASS": passes, "SKIP": skips, "FAIL": failures}[status]
            bucket.setdefault(key, []).append(detail)
        else:
            raise LedgerError(f"line {number} has unknown status {status!r}")

    required_meta = {
        "commit",
        "dirty",
        "source_sha256",
        "status_sha256",
        "source_sha256_end",
        "status_sha256_end",
        "started_at",
        "completed_at",
        "proof_mode",
        "status",
    }
    missing = sorted(required_meta - set(meta))
    if missing:
        raise LedgerError("missing terminal/source META: " + ", ".join(missing))
    if meta["status"] != "PASS":
        raise LedgerError(f"terminal status is {meta['status']!r}, not PASS")
    if failures:
        raise LedgerError("ledger contains FAIL rows: " + ", ".join(sorted(failures)))
    if meta["source_sha256"] != meta["source_sha256_end"]:
        raise LedgerError("start/end source fingerprints differ")
    if meta["status_sha256"] != meta["status_sha256_end"]:
        raise LedgerError("start/end git-status fingerprints differ")
    if "source-stability" not in passes:
        raise LedgerError("source-stability PASS is missing")
    if meta["proof_mode"] not in {"contract_only", "full_local"}:
        raise LedgerError(f"unknown proof mode {meta['proof_mode']!r}")
    return {
        "meta": meta,
        "passes": passes,
        "skips": skips,
        "failures": failures,
        "ledger_sha256": hashlib.sha256(raw).hexdigest(),
    }


def validate_ledger(
    path: Path,
    *,
    required_mode: str | None = None,
    required_passes: list[str] | None = None,
    require_current_source: bool = False,
    root: Path | None = None,
) -> dict[str, Any]:
    parsed = parse_ledger(path)
    meta = parsed["meta"]
    if required_mode and meta["proof_mode"] != required_mode:
        raise LedgerError(
            f"proof mode {meta['proof_mode']!r} cannot satisfy required mode {required_mode!r}"
        )
    missing_passes = sorted(set(required_passes or []) - set(parsed["passes"]))
    if missing_passes:
        raise LedgerError("required PASS rows missing: " + ", ".join(missing_passes))
    if require_current_source:
        try:
            current = source_fingerprint(root or Path(__file__).resolve().parent.parent)
        except FingerprintError as exc:
            raise LedgerError(f"cannot fingerprint current source: {exc}") from exc
        if current["source_sha256"] != meta["source_sha256"]:
            raise LedgerError(
                "ledger is stale for current source: "
                f"ledger={meta['source_sha256']} current={current['source_sha256']}"
            )
        if current["status_sha256"] != meta["status_sha256"]:
            raise LedgerError("ledger git-status fingerprint is stale for current source")
        parsed["current_source"] = current
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--mode", choices=("contract_only", "full_local"))
    parser.add_argument("--require-pass", action="append", default=[])
    parser.add_argument("--current-source", action="store_true")
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    try:
        result = validate_ledger(
            args.ledger,
            required_mode=args.mode,
            required_passes=args.require_pass,
            require_current_source=args.current_source,
            root=args.root,
        )
    except LedgerError as exc:
        print(f"proof-ledger: FAIL: {exc}", file=sys.stderr)
        return 1
    summary = {
        "status": "PASS",
        "proof_mode": result["meta"]["proof_mode"],
        "source_sha256": result["meta"]["source_sha256"],
        "ledger_sha256": result["ledger_sha256"],
        "pass_rows": sum(len(rows) for rows in result["passes"].values()),
        "skip_rows": sum(len(rows) for rows in result["skips"].values()),
        "required_passes": sorted(args.require_pass),
        "current_source_bound": args.current_source,
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
