#!/usr/bin/env python3
"""Pinned agent-side driver for the generalized render-speculation preview seam.

Protocol v1 is deliberately narrow:

* stdin is one bounded JSON request containing render units;
* executable code comes only from an operator-selected backend file whose SHA-256
  is pinned in the environment (never from the request);
* the existing ``cx_speculative_core.SpeculativeEngine`` runs draft -> verify ->
  accept/repair/fallback with ``measure_baseline=False``;
* stdout is one closed, preview-only JSON envelope.  It is synthetic/unattested,
  has no baseline or speedup claim, and is explicitly not billing eligible.

This is an execution seam for local experiments, not a production render job
contract.  The Rust agent independently revalidates every honesty bit before it
prints the envelope from its no-control-plane preview subcommand.

Required operator environment:

``CX_SPEC_RENDER_PREVIEW_BACKEND``
    Absolute path to a trusted Python backend module.
``CX_SPEC_RENDER_PREVIEW_BACKEND_SHA256``
    Lowercase SHA-256 of that module.
``CX_SPEC_RENDER_PREVIEW_CORE_SHA256``
    Lowercase SHA-256 of sibling ``cx_speculative_core.py``.
``CX_SPEC_RENDER_PREVIEW_ADAPTER_SHA256``
    Lowercase SHA-256 of sibling ``cx_render_spec_adapter.py``.

The backend module must declare ``PROTOCOL_VERSION = 1`` and
``MODALITY = "render"`` and expose the controller callbacks ``draft``, ``verify``,
``repair`` and ``baseline``.  It may expose ``should_speculate``.  Callback values
must use the types from ``cx_speculative_core``; the controller enforces binding,
verification, fallback, accounting, and output equality.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from types import ModuleType
from typing import Any


HERE = Path(__file__).resolve().parent
CORE_PATH = HERE / "cx_speculative_core.py"
ADAPTER_PATH = HERE / "cx_render_spec_adapter.py"

BACKEND_ENV = "CX_SPEC_RENDER_PREVIEW_BACKEND"
BACKEND_SHA_ENV = "CX_SPEC_RENDER_PREVIEW_BACKEND_SHA256"
CORE_SHA_ENV = "CX_SPEC_RENDER_PREVIEW_CORE_SHA256"
ADAPTER_SHA_ENV = "CX_SPEC_RENDER_PREVIEW_ADAPTER_SHA256"

REQUEST_KIND = "cx_spec_render_preview_request"
RESULT_KIND = "cx_spec_render_preview_result"
BRANCH_ID = "agent-render-preview-v1"
RECEIPT_TRUST = "local_experiment_unattested"

MAX_REQUEST_BYTES = 16 << 20
MAX_RESULT_BYTES = 32 << 20
MAX_UNITS = 4_096
MAX_JSON_DEPTH = 32
MAX_META_BYTES = 256 << 10


class PreviewProtocolError(ValueError):
    """A closed-protocol or content-pin violation."""


def _required_sha(name: str) -> str:
    value = os.environ.get(name, "")
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise PreviewProtocolError(
            f"{name} must be exactly 64 lowercase hexadecimal characters"
        )
    return value


def _read_pinned_source(path: Path, env_name: str) -> tuple[Path, bytes, str]:
    """Read once, hash once, and return the exact bytes that may be executed."""
    expected = _required_sha(env_name)
    try:
        canonical = path.resolve(strict=True)
        source = canonical.read_bytes()
    except OSError as exc:
        raise PreviewProtocolError(f"cannot read pinned file {path}: {exc}") from exc
    if not canonical.is_file():
        raise PreviewProtocolError(f"pinned path is not a regular file: {canonical}")
    actual = hashlib.sha256(source).hexdigest()
    if actual != expected:
        raise PreviewProtocolError(
            f"{env_name} mismatch for {canonical}: expected {expected}, got {actual}"
        )
    return canonical, source, actual


def _exec_module_from_source(name: str, path: Path, source: bytes) -> ModuleType:
    """Execute exactly the bytes already checked by `_read_pinned_source`."""
    module = ModuleType(name)
    module.__file__ = str(path)
    # Dataclasses and imports resolve the defining module through sys.modules
    # while class bodies execute, so install the exact module before exec.
    sys.modules[name] = module
    exec(compile(source, str(path), "exec"), module.__dict__)  # noqa: S102
    return module


def _load_pinned_controllers() -> tuple[ModuleType, ModuleType, str, str]:
    """Verify BOTH controller files before executing EITHER controller file."""
    core_path, core_source, core_sha = _read_pinned_source(CORE_PATH, CORE_SHA_ENV)
    adapter_path, adapter_source, adapter_sha = _read_pinned_source(
        ADAPTER_PATH, ADAPTER_SHA_ENV
    )
    previous_core = sys.modules.get("cx_speculative_core")
    previous_adapter = sys.modules.get("cx_render_spec_adapter")
    try:
        core_module = _exec_module_from_source(
            "cx_speculative_core", core_path, core_source
        )
        # cx_render_spec_adapter imports cx_speculative_core by its canonical
        # name and therefore receives the exact pinned object installed above.
        adapter_module = _exec_module_from_source(
            "cx_render_spec_adapter", adapter_path, adapter_source
        )
    except BaseException:
        # A failed load must not leave a half-initialized controller in-process
        # when tests or an embedding caller catches the exception.
        if previous_core is None:
            sys.modules.pop("cx_speculative_core", None)
        else:
            sys.modules["cx_speculative_core"] = previous_core
        if previous_adapter is None:
            sys.modules.pop("cx_render_spec_adapter", None)
        else:
            sys.modules["cx_render_spec_adapter"] = previous_adapter
        raise
    return core_module, adapter_module, core_sha, adapter_sha


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise PreviewProtocolError(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def _json_depth(value: Any) -> int:
    maximum = 0
    stack = [(value, 0)]
    while stack:
        current, parent = stack.pop()
        if isinstance(current, dict):
            depth = parent + 1
            maximum = max(maximum, depth)
            stack.extend((child, depth) for child in current.values())
        elif isinstance(current, list):
            depth = parent + 1
            maximum = max(maximum, depth)
            stack.extend((child, depth) for child in current)
    return maximum


def _canonical_json(value: Any, *, max_bytes: int) -> bytes:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise PreviewProtocolError(
            "preview value must be finite, acyclic, and JSON serializable"
        ) from exc
    if len(encoded) > max_bytes:
        raise PreviewProtocolError(
            f"canonical JSON is {len(encoded)} bytes; limit is {max_bytes}"
        )
    if _json_depth(value) > MAX_JSON_DEPTH:
        raise PreviewProtocolError(
            f"JSON nesting exceeds the {MAX_JSON_DEPTH}-level preview limit"
        )
    return encoded


def parse_request(raw: bytes) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not raw:
        raise PreviewProtocolError("preview request is empty")
    if len(raw) > MAX_REQUEST_BYTES:
        raise PreviewProtocolError(
            f"preview request is {len(raw)} bytes; limit is {MAX_REQUEST_BYTES}"
        )
    try:
        request = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except PreviewProtocolError:
        raise
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise PreviewProtocolError(f"preview request is not strict UTF-8 JSON: {exc}") from exc
    if not isinstance(request, dict):
        raise PreviewProtocolError("preview request must be a JSON object")
    allowed = {"schema_version", "kind", "units", "meta"}
    if set(request) - allowed:
        raise PreviewProtocolError(
            f"unknown preview request fields: {sorted(set(request) - allowed)}"
        )
    if request.get("schema_version") != 1 or request.get("kind") != REQUEST_KIND:
        raise PreviewProtocolError("preview request schema_version/kind mismatch")
    unit_rows = request.get("units")
    if not isinstance(unit_rows, list) or isinstance(unit_rows, (str, bytes)):
        raise PreviewProtocolError("preview request units must be an array")
    if not 1 <= len(unit_rows) <= MAX_UNITS:
        raise PreviewProtocolError(f"preview request needs 1..{MAX_UNITS} units")
    meta = request.get("meta", {})
    if not isinstance(meta, dict):
        raise PreviewProtocolError("preview request meta must be an object")
    _canonical_json(meta, max_bytes=MAX_META_BYTES)

    units: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(unit_rows):
        if not isinstance(row, dict):
            raise PreviewProtocolError(f"unit {index} must be an object")
        if set(row) != {"unit_id", "payload", "meta"}:
            raise PreviewProtocolError(
                f"unit {index} must contain exactly unit_id,payload,meta"
            )
        unit_id = row["unit_id"]
        if (
            not isinstance(unit_id, str)
            or not unit_id.strip()
            or len(unit_id.encode("utf-8")) > 256
        ):
            raise PreviewProtocolError(
                f"unit {index} unit_id must be a nonempty string <=256 UTF-8 bytes"
            )
        if unit_id in seen:
            raise PreviewProtocolError(f"duplicate unit_id {unit_id!r}")
        seen.add(unit_id)
        if not isinstance(row["meta"], dict):
            raise PreviewProtocolError(f"unit {index} meta must be an object")
        units.append(row)
    if _json_depth(request) > MAX_JSON_DEPTH:
        raise PreviewProtocolError(
            f"JSON nesting exceeds the {MAX_JSON_DEPTH}-level preview limit"
        )
    return units, meta


def _load_backend(path: Path, expected_sha256: str) -> ModuleType:
    if not path.is_absolute():
        raise PreviewProtocolError(f"{BACKEND_ENV} must be an absolute path")
    try:
        canonical = path.resolve(strict=True)
        source = canonical.read_bytes()
    except OSError as exc:
        raise PreviewProtocolError(f"cannot read backend module {path}: {exc}") from exc
    actual = hashlib.sha256(source).hexdigest()
    if actual != expected_sha256:
        raise PreviewProtocolError(
            f"{BACKEND_SHA_ENV} mismatch for {canonical}: "
            f"expected {expected_sha256}, got {actual}"
        )
    module = ModuleType("cx_pinned_render_preview_backend")
    module.__file__ = str(canonical)
    # The file is operator-authorized code.  The buyer request has no field that
    # can influence this path or module name.  Compile the exact bytes that were
    # hashed above; reopening via importlib would leave a hash-to-exec TOCTOU gap.
    exec(compile(source, str(canonical), "exec"), module.__dict__)  # noqa: S102
    if getattr(module, "PROTOCOL_VERSION", None) != 1:
        raise PreviewProtocolError("backend PROTOCOL_VERSION must equal 1")
    if getattr(module, "MODALITY", None) != "render":
        raise PreviewProtocolError("backend MODALITY must equal 'render'")
    for name in ("draft", "verify", "repair", "baseline"):
        if not callable(getattr(module, name, None)):
            raise PreviewProtocolError(f"backend callback {name!r} is missing/not callable")
    should = getattr(module, "should_speculate", None)
    if should is not None and not callable(should):
        raise PreviewProtocolError("backend should_speculate must be callable or absent")
    return module


def execute_request(raw: bytes) -> dict[str, Any]:
    # Reject the entire buyer-controlled shape before importing/executing even
    # operator-pinned backend/controller code.  Parsing is pure stdlib logic.
    unit_rows, request_meta = parse_request(raw)

    # Pin the controller implementation itself, not merely the tiny launcher.
    # No controller byte executes until both files have passed their hashes.
    core_module, render_adapter, core_sha, adapter_sha = _load_pinned_controllers()

    backend_raw = os.environ.get(BACKEND_ENV, "")
    if not backend_raw:
        raise PreviewProtocolError(f"{BACKEND_ENV} is required")
    backend_path = Path(backend_raw)
    backend_sha = _required_sha(BACKEND_SHA_ENV)
    backend = _load_backend(backend_path, backend_sha)
    units = [
        core_module.SpecUnit(
            unit_id=row["unit_id"],
            modality="render",
            payload=row["payload"],
            meta=row["meta"],
        )
        for row in unit_rows
    ]

    adapter = render_adapter.RenderSpecAdapter(
        tier=render_adapter.QualityTier(
            global_min=0.90,
            worst_tile_min=0.85,
            canonical_tier="preview",
        ),
        branch_id=BRANCH_ID,
    )
    engine = adapter.build_engine(
        draft=backend.draft,
        verify=backend.verify,
        repair=backend.repair,
        baseline=backend.baseline,
        should_speculate=getattr(backend, "should_speculate", None),
    )
    outputs, raw_receipt = engine.run(
        units,
        meta={
            "execution_path": "cx-agent/pinned-render-preview-v1",
            "preview_only": True,
            "billing_eligible": False,
            "controller_core_sha256": core_sha,
            "controller_adapter_sha256": adapter_sha,
            "backend_sha256": backend_sha,
            "request_meta": request_meta,
        },
        # Production-shaped accounting: a full reference render is an
        # authoritative lazy fallback, never an always-run speed denominator.
        measure_baseline=False,
    )
    receipt = adapter.from_speculative_receipt(
        raw_receipt,
        evidence=render_adapter.SYNTHETIC,
    ).to_dict()

    envelope = {
        "schema_version": 1,
        "kind": RESULT_KIND,
        "preview_only": True,
        "billing_eligible": False,
        "production_ready": False,
        "receipt_trust": RECEIPT_TRUST,
        "outputs": outputs,
        "receipt": receipt,
    }
    # Validate output shape/size before returning it to main (and, transitively,
    # before Rust sees any stdout).  Rust independently parses a deny-unknown-
    # fields mirror and reserializes it.
    _canonical_json(envelope, max_bytes=MAX_RESULT_BYTES)
    return envelope


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    try:
        envelope = execute_request(raw)
        sys.stdout.buffer.write(_canonical_json(envelope, max_bytes=MAX_RESULT_BYTES))
        sys.stdout.buffer.write(b"\n")
        return 0
    except Exception as exc:  # noqa: BLE001 - protocol boundary must fail closed
        print(f"cx render preview rejected: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
