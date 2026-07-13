#!/usr/bin/env python3
"""Validate high-risk outward-facing claims against the canonical claim policy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "proof" / "claims" / "claim-policy.json"


class ClaimPolicyError(ValueError):
    """The policy or an outward-facing claim violates the declared boundary."""


def _read(root: Path, relative: str) -> str:
    path = root / relative
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ClaimPolicyError(f"cannot read {relative}: {exc}") from exc


def load_policy(path: Path) -> dict[str, Any]:
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClaimPolicyError(f"cannot load policy: {exc}") from exc
    if policy.get("schema_version") != 1:
        raise ClaimPolicyError("unsupported claim-policy schema_version")
    for key in (
        "surface_discovery",
        "active_surfaces",
        "archival_surfaces",
        "internal_surfaces",
        "forbidden_active_patterns",
        "required_active_fragments",
    ):
        expected = dict if key == "surface_discovery" else list
        if not isinstance(policy.get(key), expected):
            raise ClaimPolicyError(
                f"{key} must be a {'object' if expected is dict else 'list'}"
            )
    discovery = policy["surface_discovery"]
    if set(discovery) != {"patterns", "ignored_prefixes"}:
        raise ClaimPolicyError(
            "surface_discovery must contain exactly patterns and ignored_prefixes"
        )
    for key in ("patterns", "ignored_prefixes"):
        values = discovery[key]
        if not isinstance(values, list) or not values or len(values) != len(set(values)):
            raise ClaimPolicyError(
                f"surface_discovery.{key} must be a non-empty unique string list"
            )
        if any(not isinstance(value, str) or not value for value in values):
            raise ClaimPolicyError(
                f"surface_discovery.{key} must be a non-empty unique string list"
            )
    active = policy["active_surfaces"]
    if (
        not active
        or any(not isinstance(path, str) or not path for path in active)
        or len(active) != len(set(active))
    ):
        raise ClaimPolicyError("active_surfaces must be non-empty and unique")
    ids: set[str] = set()
    for rule in policy["forbidden_active_patterns"]:
        for field in ("id", "gate", "pattern", "reason"):
            if not isinstance(rule.get(field), str) or not rule[field]:
                raise ClaimPolicyError(f"forbidden pattern needs {field}")
        if rule["id"] in ids:
            raise ClaimPolicyError(f"duplicate forbidden rule id: {rule['id']}")
        ids.add(rule["id"])
        try:
            re.compile(rule["pattern"])
        except re.error as exc:
            raise ClaimPolicyError(f"invalid regex {rule['id']}: {exc}") from exc
    return policy


def _classified_paths(rows: list[dict[str, Any]], kind: str) -> set[str]:
    paths: set[str] = set()
    for row in rows:
        for field in ("path", "required_marker"):
            if not isinstance(row.get(field), str) or not row[field]:
                raise ClaimPolicyError(f"{kind} surface needs {field}")
        if row["path"] in paths:
            raise ClaimPolicyError(f"duplicate {kind} surface: {row['path']}")
        paths.add(row["path"])
    return paths


def _discover_surfaces(policy: dict[str, Any], root: Path) -> set[str]:
    discovery = policy["surface_discovery"]
    ignored = tuple(discovery["ignored_prefixes"])
    found: set[str] = set()
    for pattern in discovery["patterns"]:
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise ClaimPolicyError(f"unsafe surface discovery pattern: {pattern!r}")
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if relative.startswith(ignored):
                continue
            found.add(relative)
    if not found:
        raise ClaimPolicyError("surface discovery found no outward files")
    return found


def validate_claims(policy: dict[str, Any], root: Path = ROOT) -> dict[str, Any]:
    errors: list[str] = []
    active_paths = set(policy["active_surfaces"])
    archival_paths = _classified_paths(policy["archival_surfaces"], "archival")
    internal_paths = _classified_paths(policy["internal_surfaces"], "internal")
    overlaps = sorted(
        (active_paths & archival_paths)
        | (active_paths & internal_paths)
        | (archival_paths & internal_paths)
    )
    if overlaps:
        raise ClaimPolicyError(
            "surfaces have multiple classifications: " + ", ".join(overlaps)
        )
    discovered = _discover_surfaces(policy, root)
    classified = active_paths | archival_paths | internal_paths
    missing = sorted(discovered - classified)
    extra = sorted(classified - discovered)
    if missing:
        errors.append("unclassified outward surfaces: " + ", ".join(missing))
    if extra:
        errors.append(
            "classified surfaces outside the fail-closed discovery set: "
            + ", ".join(extra)
        )

    texts: dict[str, str] = {}
    for relative in policy["active_surfaces"]:
        texts[relative] = _read(root, relative)

    for rule in policy["forbidden_active_patterns"]:
        regex = re.compile(rule["pattern"])
        for relative, text in texts.items():
            for match in regex.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                excerpt = match.group(0).replace("\n", " ")
                errors.append(
                    f"{relative}:{line}: {rule['id']} ({rule['gate']}): "
                    f"{excerpt!r} — {rule['reason']}"
                )

    for required in policy["required_active_fragments"]:
        for field in ("path", "gate", "fragment"):
            if not isinstance(required.get(field), str) or not required[field]:
                raise ClaimPolicyError(f"required fragment needs {field}")
        relative = required["path"]
        if relative not in texts:
            raise ClaimPolicyError(
                f"required fragment path is not an active surface: {relative}"
            )
        if required["fragment"] not in texts[relative]:
            errors.append(
                f"{relative}: missing required boundary for {required['gate']}: "
                f"{required['fragment']!r}"
            )

    for archive in policy["archival_surfaces"]:
        text = _read(root, archive["path"])
        if archive["required_marker"] not in text:
            errors.append(
                f"{archive['path']}: missing archival marker "
                f"{archive['required_marker']!r}"
            )
        for fragment in archive.get("required_fragments", []):
            if fragment not in text:
                errors.append(
                    f"{archive['path']}: missing archival warning {fragment!r}"
                )

    for internal in policy["internal_surfaces"]:
        text = _read(root, internal["path"])
        if internal["required_marker"] not in text:
            errors.append(
                f"{internal['path']}: missing internal marker "
                f"{internal['required_marker']!r}"
            )

    if errors:
        raise ClaimPolicyError("\n".join(errors))
    return {
        "status": "PASS",
        "discovered_surfaces": len(discovered),
        "active_surfaces": len(texts),
        "archival_surfaces": len(policy["archival_surfaces"]),
        "internal_surfaces": len(policy["internal_surfaces"]),
        "forbidden_rules": len(policy["forbidden_active_patterns"]),
        "required_boundaries": len(policy["required_active_fragments"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        result = validate_claims(load_policy(args.policy), args.root.resolve())
    except ClaimPolicyError as exc:
        print(f"claim-policy: FAIL:\n{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
