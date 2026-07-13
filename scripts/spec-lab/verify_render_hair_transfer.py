#!/usr/bin/env python3
"""Fail-closed verifier for the Apple Metal Fishy Cat hair-transfer proof.

The committed receipt is portable proof metadata.  When the pinned local artifact
root is still present, this verifier also hashes the manifests and all three PNGs.
Local files strengthen corroboration but are not required unless explicitly asked.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import sys
from typing import Any

import verify_render_transfer_matrix as common


HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
DEFAULT_RECEIPT = (
    REPO / "proof/performance/apple-metal-render-transfer-fishy-cat-2026-07-12.json"
)
DEFAULT_PROVENANCE = (
    REPO
    / "proof/performance/apple-metal-render-transfer-fishy-cat-provenance-2026-07-12.json"
)
RESULT_KIND = "cx_render_hair_transfer_verification"
PROVENANCE_KIND = "apple_metal_render_hair_transfer_provenance"
EXPECTED_SCENE_SHA256 = "4b5a97bed6361f1a1f888e02eda8ebda338276ff0afe46c5a170dac55bba36d5"
EXPECTED_ARCHIVE_SHA256 = "22769b4b003330eccd13de3450555bb50f2500827c4cf1997eefa365acfec275"
EXPECTED_SOURCE_URL = (
    "https://download.blender.org/demo/test/splash_fishy_cat_2.zip"
)

PROVENANCE_KEYS = {
    "schema_version",
    "kind",
    "created_at_utc",
    "status",
    "claim",
    "receipt",
    "source",
    "scene_audit",
    "selection_protocol",
    "execution",
    "quality",
    "local_artifacts",
    "limitations",
}


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_controller(
    receipt: dict[str, Any], baseline: float, product: float
) -> None:
    location = "receipt.controller_receipt"
    controller = common._object(receipt["controller_receipt"], location)
    common._exact_keys(controller, common.CONTROLLER_KEYS, location)
    expected = {
        "schema_version": 1,
        "units": 1,
        "accepted_units": 1,
        "repaired_units": 0,
        "accepted_fraction": 1.0,
        "repaired_fraction": 0.0,
        "quality_gate": True,
        "repair_cost_s": 0.0,
        "artifact_verified": False,
        "baseline_source": "measured",
        "evidence": "measured",
        "modality": "render",
        "quality_tier": "preview",
        "branch_id": "local-cycles-spec-benchmark-v1",
        "exact": False,
    }
    for key, value in expected.items():
        common._equal(controller[key], value, f"{location}.{key}")
    common._equal(controller["baseline_total_time_s"], baseline, f"{location}.baseline_total_time_s")
    common._equal(controller["total_product_time_s"], product, f"{location}.total_product_time_s")
    common._equal(controller["speedup_vs_baseline"], receipt["speedup_x"], f"{location}.speedup_vs_baseline")
    component_sum = sum(
        common._number(controller[key], f"{location}.{key}", minimum=0.0)
        for key in ("draft_cost_s", "verify_cost_s", "overhead_cost_s", "repair_cost_s")
    )
    # Each component and the independently measured total are rounded to six
    # decimals. Their displayed sum may therefore differ by one microsecond.
    if abs(component_sum - product) > 1.1e-6:
        raise common._error(location, "component costs disagree with product time beyond wire-rounding tolerance")


def _validate_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    location = "receipt"
    common._exact_keys(receipt, common.RECEIPT_KEYS, location)
    constants = {
        "schema_version": 1,
        "kind": common.RECEIPT_KIND,
        "evidence": "measured",
        "receipt_trust": "local_unattested",
        "preview_only": True,
        "production_ready": False,
        "cache_used": False,
        "cold_start_included": False,
        "trial_count": 1,
        "variance_estimate": None,
        "warmup_candidate_runs": 1,
        "sample_ranges_disjoint": True,
        "quality_gate": True,
        "quality_metric": common.QUALITY_METRIC,
        "reference_used_for_product_decision": False,
        "device": "GPU/METAL",
        "frame": 100,
        "resolution": [1920, 1080],
        "draft_samples": 7,
        "verify_samples": 7,
        "reference_samples": 4096,
        "timing_scope": "resident_steady_state_after_uncharged_warmup",
        "timing_statistics": "fixed-order single trial; no variance estimate",
    }
    for key, expected in constants.items():
        common._equal(receipt[key], expected, f"{location}.{key}")
    common._equal(
        receipt["execution_order"],
        [
            "uncharged_full_candidate_warmup",
            "measured_baseline",
            "measured_candidate",
            "measurement_only_baseline_audit",
        ],
        f"{location}.execution_order",
    )
    common._equal(
        receipt["order_bias_control"],
        "full candidate path warmed before baseline",
        f"{location}.order_bias_control",
    )
    common._equal(
        receipt["scene_sha256"], EXPECTED_SCENE_SHA256, f"{location}.scene_sha256"
    )

    ranges = common._ranges(receipt["sample_ranges"], f"{location}.sample_ranges")
    common._equal(
        ranges,
        {"draft": (0, 7), "verify": (7, 14), "baseline": (14, 4110)},
        f"{location}.sample_ranges",
    )
    baseline = common._number(receipt["baseline_s"], f"{location}.baseline_s", minimum=0.000001)
    product = common._number(receipt["spec_s"], f"{location}.spec_s", minimum=0.000001)
    speedup = common._number(receipt["speedup_x"], f"{location}.speedup_x", minimum=0.0)
    common._equal(speedup, round(baseline / product, 6), f"{location}.speedup_x")
    common._equal(receipt["meets_50x_preview_experiment"], speedup >= 50.0, f"{location}.meets_50x_preview_experiment")
    common._equal(receipt["meets_100x_preview_experiment"], speedup >= 100.0, f"{location}.meets_100x_preview_experiment")
    if speedup < 50.0:
        raise common._error(location, "does not meet the 50x preview threshold")

    output = common._validate_output_descriptor(receipt, location)
    _validate_controller(receipt, baseline, product)
    audit = common._validate_audit(receipt, ranges, output, location)

    worker = common._object(receipt["worker_renderer_identity"], f"{location}.worker_renderer_identity")
    common._exact_keys(
        worker,
        {
            "blender_build_hash",
            "blender_version",
            "dependency_count",
            "dependency_paths_sha256",
            "device",
            "enabled_device_names",
            "png_compression",
        },
        f"{location}.worker_renderer_identity",
    )
    common._equal(worker["device"], "GPU/METAL", f"{location}.worker_renderer_identity.device")
    common._equal(worker["png_compression"], 0, f"{location}.worker_renderer_identity.png_compression")
    enabled = common._array(worker["enabled_device_names"], f"{location}.worker_renderer_identity.enabled_device_names")
    common._equal(enabled, ["Apple M3 Ultra (GPU - 60 cores)"], f"{location}.worker_renderer_identity.enabled_device_names")
    pins = common._object(receipt["pins"], f"{location}.pins")
    backend_sha = common._sha256(pins.get("backend_sha256"), f"{location}.pins.backend_sha256")

    return {
        "baseline_s": baseline,
        "product_s": product,
        "speedup_x": speedup,
        "ranges": ranges,
        "output": output,
        "audit": audit,
        "backend_sha256": backend_sha,
    }


def _validate_provenance(
    provenance: dict[str, Any],
    provenance_bytes: bytes,
    receipt: dict[str, Any],
    receipt_bytes: bytes,
    checked: dict[str, Any],
) -> dict[str, Any]:
    location = "provenance"
    common._exact_keys(provenance, PROVENANCE_KEYS, location)
    common._equal(provenance["schema_version"], 1, f"{location}.schema_version")
    common._equal(provenance["kind"], PROVENANCE_KIND, f"{location}.kind")
    common._equal(provenance["status"], "accepted_preview_experiment", f"{location}.status")
    common._string(provenance["created_at_utc"], f"{location}.created_at_utc")

    receipt_pin = common._object(provenance["receipt"], f"{location}.receipt")
    common._exact_keys(
        receipt_pin,
        {"path", "sha256", "canonical_json_sha256", "canonical_json_method"},
        f"{location}.receipt",
    )
    common._equal(receipt_pin["sha256"], _digest(receipt_bytes), f"{location}.receipt.sha256")
    common._equal(
        receipt_pin["canonical_json_sha256"],
        _digest(_canonical_bytes(receipt)),
        f"{location}.receipt.canonical_json_sha256",
    )
    common._equal(
        receipt_pin["canonical_json_method"],
        "Python json.dumps(sort_keys=True,separators=(',',':'),ensure_ascii=False) followed by one LF byte",
        f"{location}.receipt.canonical_json_method",
    )

    claim = common._object(provenance["claim"], f"{location}.claim")
    common._equal(claim.get("fresh_held_out_frame"), 100, f"{location}.claim.fresh_held_out_frame")
    common._equal(claim.get("measured_same_session_speedup_x"), checked["speedup_x"], f"{location}.claim.measured_same_session_speedup_x")
    common._equal(claim.get("baseline_seconds"), checked["baseline_s"], f"{location}.claim.baseline_seconds")
    common._equal(claim.get("product_seconds"), checked["product_s"], f"{location}.claim.product_seconds")
    common._equal(claim.get("meets_50x_preview_experiment"), True, f"{location}.claim.meets_50x_preview_experiment")
    common._equal(claim.get("meets_100x_preview_experiment"), False, f"{location}.claim.meets_100x_preview_experiment")
    common._equal(claim.get("production_claim_authorized"), False, f"{location}.claim.production_claim_authorized")

    source = common._object(provenance["source"], f"{location}.source")
    common._equal(source.get("url"), EXPECTED_SOURCE_URL, f"{location}.source.url")
    common._equal(source.get("archive_sha256"), EXPECTED_ARCHIVE_SHA256, f"{location}.source.archive_sha256")
    common._equal(source.get("scene_sha256"), EXPECTED_SCENE_SHA256, f"{location}.source.scene_sha256")
    common._integer(source.get("archive_bytes"), f"{location}.source.archive_bytes", minimum=1)
    common._integer(source.get("scene_bytes"), f"{location}.source.scene_bytes", minimum=1)

    scene = common._object(provenance["scene_audit"], f"{location}.scene_audit")
    common._equal(scene.get("engine"), "CYCLES", f"{location}.scene_audit.engine")
    common._equal(scene.get("frame_end"), 100, f"{location}.scene_audit.frame_end")
    systems = common._array(scene.get("particle_systems"), f"{location}.scene_audit.particle_systems")
    hair_count = sum(
        common._integer(row.get("count"), f"{location}.scene_audit.particle_systems[{index}].count", minimum=0)
        for index, row in enumerate(systems)
        if common._object(row, f"{location}.scene_audit.particle_systems[{index}]").get("type") == "HAIR"
    )
    if hair_count < 1:
        raise common._error(f"{location}.scene_audit", "contains no audited hair particles")

    selection = common._object(provenance["selection_protocol"], f"{location}.selection_protocol")
    common._equal(selection.get("calibration_frames"), [1, 50], f"{location}.selection_protocol.calibration_frames")
    if 100 in selection.get("calibration_frames", []):
        raise common._error(f"{location}.selection_protocol", "held-out frame appears in calibration")
    common._equal(selection.get("frozen_samples"), {"draft": 7, "verify": 7, "reference": 4096}, f"{location}.selection_protocol.frozen_samples")

    execution = common._object(provenance["execution"], f"{location}.execution")
    common._equal(execution.get("backend_sha256"), checked["backend_sha256"], f"{location}.execution.backend_sha256")
    common._equal(execution.get("png_compression"), 0, f"{location}.execution.png_compression")
    common._equal(execution.get("sample_ranges"), receipt["sample_ranges"], f"{location}.execution.sample_ranges")
    common._equal(execution.get("repair_used"), False, f"{location}.execution.repair_used")
    common._equal(execution.get("cache_used"), False, f"{location}.execution.cache_used")

    quality = common._object(provenance["quality"], f"{location}.quality")
    product_global = common._number(quality.get("product_global_agreement"), f"{location}.quality.product_global_agreement", minimum=0.0, maximum=1.0)
    product_regional = common._number(quality.get("product_worst_regional_agreement"), f"{location}.quality.product_worst_regional_agreement", minimum=0.0, maximum=1.0)
    product_micro = common._number(quality.get("product_worst_microtile_agreement"), f"{location}.quality.product_worst_microtile_agreement", minimum=0.0, maximum=1.0)
    product_passed = (
        product_global >= common.GLOBAL_MIN
        and product_regional >= common.REGIONAL_MIN
        and product_micro >= common.MICROTILE_MIN
    )
    common._equal(quality.get("product_gate_passed"), product_passed, f"{location}.quality.product_gate_passed")
    if not product_passed:
        raise common._error(f"{location}.quality", "reference-free product gate did not pass")
    common._equal(quality.get("global_agreement"), checked["audit"]["global"], f"{location}.quality.global_agreement")
    common._equal(quality.get("worst_regional_agreement"), checked["audit"]["regional"], f"{location}.quality.worst_regional_agreement")
    common._equal(quality.get("worst_microtile_agreement"), checked["audit"]["micro"], f"{location}.quality.worst_microtile_agreement")
    common._equal(quality.get("fresh_4096_audit_passed"), True, f"{location}.quality.fresh_4096_audit_passed")

    limitations = common._array(provenance["limitations"], f"{location}.limitations")
    if len(limitations) < 5 or any(not isinstance(item, str) or not item for item in limitations):
        raise common._error(f"{location}.limitations", "must contain at least five non-empty caveats")
    common._object(provenance["local_artifacts"], f"{location}.local_artifacts")
    common._sha256(_digest(provenance_bytes), "provenance.sha256")
    return {"hair_particles": hair_count}


def _manifest_artifact(
    root: Path, manifest: dict[str, Any], location: str
) -> tuple[Path, bytes]:
    artifact = common._object(manifest.get("artifact"), f"{location}.artifact")
    path = common._contained_file(root, artifact.get("path"), f"{location}.artifact.path")
    data = common._stable_bytes(path, f"{location}.artifact", maximum=common.MAX_ARTIFACT_BYTES)
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise common._error(f"{location}.artifact", "is not a PNG")
    common._equal(artifact.get("sha256"), _digest(data), f"{location}.artifact.sha256")
    return path, data


def _verify_local_artifacts(
    receipt: dict[str, Any], provenance: dict[str, Any], *, require_local: bool
) -> bool:
    root = Path(common._string(receipt["artifact_root"], "receipt.artifact_root"))
    if not root.exists():
        if require_local:
            raise common._error("receipt.artifact_root", "local artifacts are required but absent")
        return False
    if root.is_symlink() or not root.is_dir():
        raise common._error("receipt.artifact_root", "must be a non-symlink directory")

    output = receipt["outputs"][0]
    draft_path = common._contained_file(root, output["manifest_path"], "local.draft_manifest")
    draft, draft_bytes = common._read_json(draft_path, "local.draft_manifest")
    audit = receipt["benchmark_audit"]
    baseline_path = common._contained_file(root, audit["baseline"]["manifest_path"], "local.baseline_manifest")
    baseline, baseline_bytes = common._read_json(baseline_path, "local.baseline_manifest")
    unit_relative = PurePosixPath(output["manifest_path"]).parent
    verification_path = common._contained_file(root, str(unit_relative / "verification-manifest.json"), "local.verification_manifest")
    verification, verification_bytes = common._read_json(verification_path, "local.verification_manifest")
    audit_path = common._contained_file(root, str(unit_relative / "benchmark-audit.json"), "local.benchmark_audit")
    local_audit, audit_bytes = common._read_json(audit_path, "local.benchmark_audit")

    common._equal(local_audit, audit, "local.benchmark_audit")
    common._equal(draft.get("phase"), "draft", "local.draft_manifest.phase")
    common._equal(baseline.get("phase"), "baseline", "local.baseline_manifest.phase")
    common._equal(draft.get("binding_sha256"), audit["binding_sha256"], "local.draft_manifest.binding_sha256")
    common._equal(baseline.get("binding_sha256"), audit["binding_sha256"], "local.baseline_manifest.binding_sha256")
    common._equal(verification.get("binding_sha256"), audit["binding_sha256"], "local.verification_manifest.binding_sha256")
    common._equal(verification.get("accepted"), True, "local.verification_manifest.accepted")
    common._equal(verification.get("selected_manifest_path"), output["manifest_path"], "local.verification_manifest.selected_manifest_path")
    for name, manifest in (("draft", draft), ("baseline", baseline)):
        common._equal(
            manifest.get("pins"),
            {
                "blender_sha256": receipt["pins"]["blender_sha256"],
                "backend_sha256": receipt["pins"]["backend_sha256"],
                "child_script_sha256": manifest["pins"]["child_script_sha256"],
                "controller_core_sha256": receipt["pins"]["controller_core_sha256"],
                "controller_adapter_sha256": receipt["pins"]["controller_adapter_sha256"],
            },
            f"local.{name}_manifest.pins",
        )
        common._sha256(
            manifest["pins"]["child_script_sha256"],
            f"local.{name}_manifest.pins.child_script_sha256",
        )
        common._equal(
            manifest.get("render", {}).get("png_compression"),
            0,
            f"local.{name}_manifest.render.png_compression",
        )
        common._equal(
            manifest.get("render", {}).get("worker_renderer_identity"),
            receipt["worker_renderer_identity"],
            f"local.{name}_manifest.render.worker_renderer_identity",
        )
    common._equal(
        draft["pins"]["child_script_sha256"],
        baseline["pins"]["child_script_sha256"],
        "local.manifest_child_script_sha256",
    )

    draft_png_path, draft_png = _manifest_artifact(root, draft, "local.draft_manifest")
    baseline_png_path, baseline_png = _manifest_artifact(root, baseline, "local.baseline_manifest")
    verify_artifact = common._object(verification.get("verify_artifact"), "local.verification_manifest.verify_artifact")
    verify_png_path = common._contained_file(root, verify_artifact.get("path"), "local.verification_manifest.verify_artifact.path")
    verify_png = common._stable_bytes(verify_png_path, "local.verify_png", maximum=common.MAX_ARTIFACT_BYTES)
    common._equal(verify_artifact.get("sha256"), _digest(verify_png), "local.verification_manifest.verify_artifact.sha256")

    pins = common._object(provenance["local_artifacts"], "provenance.local_artifacts")
    expected_manifest_hashes = {
        "draft_manifest_sha256": _digest(draft_bytes),
        "baseline_manifest_sha256": _digest(baseline_bytes),
        "verification_manifest_sha256": _digest(verification_bytes),
        "benchmark_audit_sha256": _digest(audit_bytes),
    }
    for key, actual in expected_manifest_hashes.items():
        common._equal(pins.get(key), actual, f"provenance.local_artifacts.{key}")
    for key, path, data in (
        ("draft_png", draft_png_path, draft_png),
        ("verify_png", verify_png_path, verify_png),
        ("baseline_png", baseline_png_path, baseline_png),
    ):
        row = common._object(pins.get(key), f"provenance.local_artifacts.{key}")
        common._equal(row.get("bytes"), len(data), f"provenance.local_artifacts.{key}.bytes")
        common._equal(row.get("sha256"), _digest(data), f"provenance.local_artifacts.{key}.sha256")
    return True


def verify(
    receipt_path: Path = DEFAULT_RECEIPT,
    provenance_path: Path = DEFAULT_PROVENANCE,
    *,
    require_local_artifacts: bool = False,
) -> dict[str, Any]:
    receipt, receipt_bytes = common._read_json(receipt_path, "receipt")
    provenance, provenance_bytes = common._read_json(provenance_path, "provenance")
    checked = _validate_receipt(receipt)
    provenance_checked = _validate_provenance(
        provenance, provenance_bytes, receipt, receipt_bytes, checked
    )
    local = _verify_local_artifacts(
        receipt, provenance, require_local=require_local_artifacts
    )
    return {
        "schema_version": 1,
        "kind": RESULT_KIND,
        "valid": True,
        "receipt_sha256": _digest(receipt_bytes),
        "provenance_sha256": _digest(provenance_bytes),
        "speedup_x": checked["speedup_x"],
        "meets_50x_preview_experiment": True,
        "hair_particles_audited": provenance_checked["hair_particles"],
        "local_artifacts_verified": local,
        "production_ready": False,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument("--provenance", type=Path, default=DEFAULT_PROVENANCE)
    parser.add_argument("--require-local-artifacts", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = verify(
            args.receipt,
            args.provenance,
            require_local_artifacts=args.require_local_artifacts,
        )
    except Exception as exc:  # noqa: BLE001 - one fail-closed CLI envelope
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": RESULT_KIND,
                    "valid": False,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
