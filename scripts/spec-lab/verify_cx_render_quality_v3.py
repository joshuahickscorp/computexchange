#!/usr/bin/env python3
"""Fail-closed verifier for a frozen ``cx_render_quality_v3`` result.

The verifier requires explicit candidate and reference artifact paths because a
v3 result binds their bytes and SHA-256 digests but does not bind filesystem
paths.  It reads each regular, non-symlink file once, verifies the recorded
contract and evaluator runtime identity, reruns the evaluator from those exact
bytes, and requires canonical equality with the supplied result.  Canonical
equality is deliberately stricter than a floating-point tolerance: v3 metrics
are already rounded to their nine-decimal wire representation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys
import types
from typing import Any, Sequence


VERIFIER_ID = "cx-render-preview-quality-v3-verifier-v1"
RESULT_KIND = "cx_render_quality_v3_verification"
SCHEMA_VERSION = 1
PROOF_RESULT_KIND = "cx_render_quality_v3_result"
PROOF_SCHEMA_VERSION = 3
EXPECTED_CONTRACT_ID = "cx-render-preview-quality-v3"
EXPECTED_CONTRACT_SHA256 = (
    "6665e2931fed124108929bfd9cfc093c6db69407fcd7fc8a39644c4e39183b0b"
)
EXPECTED_EVALUATOR_SHA256 = (
    "819d5b3c2ba6da2b67e3e9feffece242e7b17d2b55f858d783962b0048c81c5e"
)
MAX_PROOF_BYTES = 4 * 1024 * 1024
quality: Any | None = None
EXPECTED_RESULT_KEYS = frozenset(
    {
        "alpha_agreement",
        "contract",
        "contract_sha256",
        "errors",
        "failures",
        "inputs",
        "kind",
        "mattes",
        "pass",
        "runtime",
        "schema_version",
    }
)
EXPECTED_INPUT_KEYS = frozenset(
    {"candidate", "reference", "target_dimensions"}
)
EXPECTED_ARTIFACT_KEYS = frozenset({"bytes", "mode", "sha256"})


class VerificationInputError(ValueError):
    """A deterministic verifier input failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise VerificationInputError("proof_not_canonical_json") from exc


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise VerificationInputError("evaluator_identity_unreadable") from exc
    return digest.hexdigest()


def _file_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
        info.st_gid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _read_regular_file(
    path_value: str | Path, *, minimum: int, maximum: int, label: str
) -> bytes:
    path = Path(path_value)
    try:
        path_info = path.lstat()
    except OSError as exc:
        raise VerificationInputError(f"{label}_unreadable") from exc
    if not stat.S_ISREG(path_info.st_mode) or path.is_symlink():
        raise VerificationInputError(f"{label}_not_regular")
    if not minimum <= path_info.st_size <= maximum:
        raise VerificationInputError(f"{label}_size_invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise VerificationInputError(f"{label}_unreadable") from exc
    chunks: list[bytes] = []
    total = 0
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise VerificationInputError(f"{label}_not_regular")
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise VerificationInputError(f"{label}_size_invalid")
            chunks.append(chunk)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise VerificationInputError(f"{label}_unreadable") from exc
    finally:
        os.close(descriptor)
    if _file_identity(before) != _file_identity(after) or total != after.st_size:
        raise VerificationInputError(f"{label}_changed_during_read")
    try:
        current = path.lstat()
    except OSError as exc:
        raise VerificationInputError(f"{label}_changed_during_read") from exc
    if path.is_symlink() or _file_identity(current) != _file_identity(after):
        raise VerificationInputError(f"{label}_changed_during_read")
    return b"".join(chunks)


def _load_pinned_evaluator() -> Any:
    """Hash the exact evaluator source bytes before executing those bytes."""

    global quality
    source_path = Path(__file__).resolve(strict=True).with_name(
        "cx_render_quality_v3.py"
    )
    source = _read_regular_file(
        source_path,
        minimum=1,
        maximum=MAX_PROOF_BYTES,
        label="evaluator_source",
    )
    if _sha256_bytes(source) != EXPECTED_EVALUATOR_SHA256:
        raise VerificationInputError("evaluator_source_pin_mismatch")
    if quality is not None:
        return quality
    module = types.ModuleType("_cx_render_quality_v3_pinned")
    module.__file__ = str(source_path)
    module.__package__ = ""
    try:
        code = compile(source, str(source_path), "exec")
        exec(code, module.__dict__)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:
        raise VerificationInputError("evaluator_import_failed") from exc
    quality = module
    return module


def _reject_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise VerificationInputError("proof_duplicate_key")
        result[key] = value
    return result


def _finite_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise VerificationInputError("proof_nonfinite_number") from exc
    if not math.isfinite(value):
        raise VerificationInputError("proof_nonfinite_number")
    return value


def _reject_constant(_raw: str) -> None:
    raise VerificationInputError("proof_nonfinite_number")


def _parse_proof(data: bytes) -> dict[str, Any]:
    try:
        text = data.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except VerificationInputError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise VerificationInputError("proof_json_invalid") from exc
    if not isinstance(value, dict):
        raise VerificationInputError("proof_not_object")
    return value


def _contract_sha256(module: Any) -> str:
    return _sha256_bytes(_canonical_json(module.CONTRACT_DESCRIPTOR))


def _current_evaluator_identity() -> dict[str, Any]:
    module = _load_pinned_evaluator()
    module_path = Path(module.__file__).resolve(strict=True)
    expected_path = Path(__file__).resolve(strict=True).with_name(
        "cx_render_quality_v3.py"
    )
    if module_path != expected_path:
        raise VerificationInputError("evaluator_module_path_mismatch")
    runtime = module.runtime_identity()
    module_sha256 = _sha256_file(module_path)
    if runtime.get("metric_module_sha256") != module_sha256:
        raise VerificationInputError("evaluator_identity_inconsistent")
    if module_sha256 != EXPECTED_EVALUATOR_SHA256:
        raise VerificationInputError("evaluator_source_pin_mismatch")
    contract_sha256 = _contract_sha256(module)
    if (
        module.CONTRACT_ID != EXPECTED_CONTRACT_ID
        or contract_sha256 != EXPECTED_CONTRACT_SHA256
    ):
        raise VerificationInputError("evaluator_contract_pin_mismatch")
    return {
        "contract_sha256": contract_sha256,
        "dependency_tree_sha256": runtime.get("dependency_tree_sha256"),
        "metric_module_sha256": module_sha256,
    }


def _base_output(evaluator: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": RESULT_KIND,
        "schema_version": SCHEMA_VERSION,
        "verifier": VERIFIER_ID,
        "comparison": "canonical_exact_after_v3_nine_decimal_wire_rounding",
        "evaluator": evaluator,
        "proof_result_sha256": None,
        "recomputed_result_sha256": None,
        "artifacts": {
            "candidate": {"bytes": None, "sha256": None},
            "reference": {"bytes": None, "sha256": None},
        },
        "proof_verified": False,
        "quality_pass": None,
        "errors": [],
        "pass": False,
    }


def _reject(output: dict[str, Any], code: str) -> dict[str, Any]:
    output["errors"] = [code]
    return output


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_proof_header(
    proof: dict[str, Any], evaluator: dict[str, Any]
) -> str | None:
    if frozenset(proof) != EXPECTED_RESULT_KEYS:
        return "proof_result_shape_mismatch"
    if proof.get("kind") != PROOF_RESULT_KIND:
        return "proof_result_kind_mismatch"
    if (
        type(proof.get("schema_version")) is not int
        or proof["schema_version"] != PROOF_SCHEMA_VERSION
    ):
        return "proof_result_schema_mismatch"
    try:
        contract_equal = _canonical_json(proof.get("contract")) == _canonical_json(
            quality.CONTRACT_DESCRIPTOR
        )
    except VerificationInputError:
        return "proof_contract_identity_mismatch"
    if not contract_equal or proof.get("contract_sha256") != evaluator["contract_sha256"]:
        return "proof_contract_identity_mismatch"
    try:
        runtime_equal = _canonical_json(proof.get("runtime")) == _canonical_json(
            quality.runtime_identity()
        )
    except VerificationInputError:
        return "proof_evaluator_identity_mismatch"
    if not runtime_equal:
        return "proof_evaluator_identity_mismatch"
    inputs = proof.get("inputs")
    if not isinstance(inputs, dict) or frozenset(inputs) != EXPECTED_INPUT_KEYS:
        return "proof_inputs_shape_mismatch"
    dimensions = inputs.get("target_dimensions")
    if (
        not isinstance(dimensions, list)
        or len(dimensions) != 2
        or any(type(value) is not int for value in dimensions)
        or any(value <= 0 for value in dimensions)
        or dimensions[0] * dimensions[1] > quality.MAX_PIXELS
    ):
        return "proof_target_dimensions_invalid"
    for label in ("candidate", "reference"):
        row = inputs.get(label)
        if not isinstance(row, dict) or frozenset(row) != EXPECTED_ARTIFACT_KEYS:
            return f"proof_{label}_binding_shape_mismatch"
        if (
            type(row.get("bytes")) is not int
            or row["bytes"] < 0
            or row["bytes"] > quality.MAX_PNG_BYTES
            or not _valid_sha256(row.get("sha256"))
            or row.get("mode") not in {None, "RGB", "RGBA"}
        ):
            return f"proof_{label}_binding_invalid"
    if type(proof.get("pass")) is not bool:
        return "proof_decision_type_mismatch"
    if not isinstance(proof.get("failures"), list) or not all(
        isinstance(value, str) for value in proof["failures"]
    ):
        return "proof_failures_shape_mismatch"
    if not isinstance(proof.get("errors"), list) or not all(
        isinstance(value, str) for value in proof["errors"]
    ):
        return "proof_errors_shape_mismatch"
    return None


def verify_paths(
    proof_path: str | Path,
    candidate_path: str | Path,
    reference_path: str | Path,
) -> dict[str, Any]:
    """Verify one v3 result and its explicitly supplied artifact files."""

    try:
        evaluator = _current_evaluator_identity()
    except VerificationInputError as exc:
        evaluator = {
            "contract_sha256": None,
            "dependency_tree_sha256": None,
            "metric_module_sha256": None,
        }
        return _reject(_base_output(evaluator), exc.code)
    except OSError:
        evaluator = {
            "contract_sha256": None,
            "dependency_tree_sha256": None,
            "metric_module_sha256": None,
        }
        return _reject(_base_output(evaluator), "evaluator_identity_unavailable")
    output = _base_output(evaluator)
    try:
        proof_data = _read_regular_file(
            proof_path, minimum=1, maximum=MAX_PROOF_BYTES, label="proof"
        )
        proof = _parse_proof(proof_data)
        canonical_proof = _canonical_json(proof)
    except VerificationInputError as exc:
        return _reject(output, exc.code)
    output["proof_result_sha256"] = _sha256_bytes(canonical_proof)

    header_error = _validate_proof_header(proof, evaluator)
    if header_error is not None:
        return _reject(output, header_error)

    artifact_data: dict[str, bytes] = {}
    for label, path in (
        ("candidate", candidate_path),
        ("reference", reference_path),
    ):
        try:
            data = _read_regular_file(
                path, minimum=0, maximum=quality.MAX_PNG_BYTES, label=label
            )
        except VerificationInputError as exc:
            return _reject(output, exc.code)
        artifact_data[label] = data
        observed = {"bytes": len(data), "sha256": _sha256_bytes(data)}
        output["artifacts"][label] = observed
        claimed = proof["inputs"][label]
        if (
            claimed["bytes"] != observed["bytes"]
            or claimed["sha256"] != observed["sha256"]
        ):
            return _reject(output, f"{label}_artifact_binding_mismatch")

    dimensions = proof["inputs"]["target_dimensions"]
    try:
        recomputed = quality.evaluate_png_bytes(
            artifact_data["candidate"],
            artifact_data["reference"],
            target_size=(dimensions[0], dimensions[1]),
        )
        canonical_recomputed = _canonical_json(recomputed)
    except KeyboardInterrupt:
        raise
    except BaseException:
        # The public evaluator is intended to fail closed. An escaped exception
        # makes the proof unverifiable regardless of its type or message.
        return _reject(output, "evaluator_execution_failed")
    output["recomputed_result_sha256"] = _sha256_bytes(canonical_recomputed)
    if canonical_proof != canonical_recomputed:
        return _reject(output, "proof_recomputation_mismatch")

    output["proof_verified"] = True
    output["quality_pass"] = recomputed["pass"]
    output["pass"] = bool(recomputed["pass"])
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a render-quality-v3 result against explicit candidate and "
            "reference PNG artifacts."
        )
    )
    parser.add_argument("proof", help="render-quality-v3 result JSON")
    parser.add_argument("candidate", help="candidate PNG bound by the result")
    parser.add_argument("reference", help="reference PNG bound by the result")
    parser.add_argument(
        "--pretty", action="store_true", help="indent deterministic JSON output"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = verify_paths(args.proof, args.candidate, args.reference)
    if args.pretty:
        payload = json.dumps(
            result,
            sort_keys=True,
            indent=2,
            ensure_ascii=True,
            allow_nan=False,
        )
    else:
        payload = _canonical_json(result).decode("ascii")
    sys.stdout.write(payload + "\n")
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
