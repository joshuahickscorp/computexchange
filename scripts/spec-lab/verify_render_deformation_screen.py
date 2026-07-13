#!/usr/bin/env python3
"""Fail-closed verifier for the Stylized Levi deformation slope screen.

The committed proof is deliberately negative: five calibration runs are
measured, but its 4096-spp baseline and 35.697660x ratio are a two-point,
cross-run linear extrapolation.  Cache paths are portable and optional.  When
present, every receipt, decisive manifest, audit, and PNG is hashed and the
quality and inter-frame image metrics are recomputed.
"""

from __future__ import annotations

import argparse
import copy
from io import BytesIO
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
from typing import Any

import verify_render_transfer_matrix as common


HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
DEFAULT_PROOF = (
    REPO
    / "proof/performance/"
    "apple-metal-render-deformation-stylized-levi-screen-2026-07-12.json"
)
PROOF_KIND = "cx_local_cycles_deformation_screen"
RESULT_KIND = "cx_render_deformation_screen_verification"
EXPECTED_SEMANTIC_SHA256 = (
    "f485cc9e34d46713632848ea4bad66843be095c2082d22ac6929baacb6dc4b36"
)

SOURCE_URL = "https://download.blender.org/demo/test/stylized_levi.zip"
DERIVATIVE_SHA256 = (
    "5017ff5d088db73d9e3e5b2b8cfdf28c0db7581c74c196fcdf006b4dfec62684"
)
SOURCE_DESCRIPTORS = {
    "archive": {
        "bytes": 13849620,
        "sha256": (
            "9b529ecefe2659f4b9ff92e43f4fab7efaea3f99524b359b077655b981a85fd1"
        ),
    },
    "original_scene": {
        "bytes": 39566840,
        "sha256": (
            "5c0a4a6f282483d159431ebe71c9c249ecbb18a2f95ed3abf05d7fa083a72e80"
        ),
    },
    "derivative": {"bytes": 35137368, "sha256": DERIVATIVE_SHA256},
    "builder": {
        "path": "scripts/spec-lab/make_stylized_levi_deformation_derivative.py",
        "bytes": 11531,
        "sha256": (
            "48ace9f8f6f417dbd5b9b2d961b1116e4151d9324da3adc99ad4a63b01f7297a"
        ),
    },
}
DERIVATIVE_AUDIT = {
    "engine": "CYCLES",
    "camera": "Camera.001",
    "frames": [6, 7],
    "rig": "male_metarig",
    "action": "pose_test",
    "action_fcurves": 397,
    "armature_modifiers": 41,
    "all_armature_modifiers_target_rig": True,
    "lattice_modifiers": 6,
    "pose_frame_6_sha256": (
        "bb9a5531236c15e0d1123b9fe6f21a72c8cd76b80f8f34171de5c79230f5a6e0"
    ),
    "pose_frame_7_sha256": (
        "5b96ecdee343df5349f3e1e3d81aab0585956ba68981c2a225edc15aefe5e8c3"
    ),
    "changed_pose_bones": 69,
    "max_pose_matrix_element_delta": 2.083181143,
    "converted_materials": 54,
    "remaining_undefined_material_nodes": 0,
    "remaining_node_groups": 0,
    "removed_unused_external_images": 1,
    "removed_image_users": 0,
    "appearance_equivalent_to_original": False,
}
QUALITY_CONTRACT = {
    "metric": "one_minus_mean_absolute_rgb_difference",
    "global_min": 0.9,
    "worst_regional_min": 0.85,
    "worst_microtile_min": 0.7,
    "regional_grid": [16, 9],
    "microtile_nominal_edge_pixels": 32,
    "reference_used_for_product_decision": False,
}
PINS = {
    "backend_sha256": (
        "1e1b156663a0d3242874294a87194de59cc283ffb1f983106e15ed6a52e94e22"
    ),
    "benchmark_harness_sha256": (
        "22b6e0e43c2c694466b625f10398b114bd0a2cd6638884ab0d1fb6429e086790"
    ),
    "blender_sha256": (
        "b5737da97b0e164cc18e227be115c4ea2791d11e9577f7e302c73f4871f9249c"
    ),
    "controller_adapter_sha256": (
        "fbd72047b0497a6ce886c22bb86ba4a4b374a5ebd05b86cc1eb4c2d91afd534e"
    ),
    "controller_core_sha256": (
        "c729c8b9e5d231f9c6566d1bc8c492af2ecad4fc2aade7cf2a188f35428d9a11"
    ),
    "render_preview_driver_sha256": (
        "5d05da0214b9dab5446910a303691b2a5b342a6658a22e1d4aa0faf07abc1e6b"
    ),
}
RUN_ORDER = [
    "f6_512_r128_d2_v2",
    "f7_512_r128_d2_v2",
    "f6_1080_r256_d1_v1",
    "f7_1080_r256_d1_v1",
    "f7_1080_r512_d1_v1",
]
RUN_CONFIGS = {
    "f6_512_r128_d2_v2": (6, [512, 288], 2, 2, 128),
    "f7_512_r128_d2_v2": (7, [512, 288], 2, 2, 128),
    "f6_1080_r256_d1_v1": (6, [1920, 1080], 1, 1, 256),
    "f7_1080_r256_d1_v1": (7, [1920, 1080], 1, 1, 256),
    "f7_1080_r512_d1_v1": (7, [1920, 1080], 1, 1, 512),
}
FILE_NAMES = {
    "draft-manifest.json",
    "verification-manifest.json",
    "baseline-manifest.json",
    "benchmark-audit.json",
    "draft.png",
    "verify.png",
    "baseline.png",
}

PROOF_KEYS = {
    "schema_version",
    "kind",
    "created_at_utc",
    "status",
    "evidence",
    "receipt_trust",
    "preview_only",
    "production_ready",
    "claim_scope",
    "source",
    "derivative_audit",
    "quality_contract",
    "protocol",
    "deformation_change",
    "slope_screen",
    "runs",
    "limitations",
}
SOURCE_KEYS = {"url", "archive", "original_scene", "derivative", "builder"}
DESCRIPTOR_KEYS = {"path", "bytes", "sha256"}
PROTOCOL_KEYS = {
    "run_count",
    "run_order",
    "timing_scope",
    "timing_statistics",
    "cold_start_included",
    "cache_used",
    "renderer",
    "device",
    "enabled_device_name",
    "manifest_schema_version",
    "audit_schema_version",
    "verification_schema_version",
    "pins",
    "projection_label",
}
CHANGE_KEYS = {
    "run_keys",
    "artifact",
    "resolution",
    "global_agreement",
    "worst_regional_agreement",
    "worst_microtile_agreement",
    "nonblack_test",
    "frame_6_nonblack_fraction",
    "frame_7_nonblack_fraction",
    "meaningful_full_frame_change",
}
SLOPE_KEYS = {
    "method",
    "baseline_points",
    "slope_seconds_per_sample",
    "intercept_seconds",
    "target_samples",
    "projected_baseline_s",
    "product_run_key",
    "product_s",
    "projected_speedup_x",
    "measured_speedup",
    "measured_4096_baseline",
    "meets_50x",
    "meets_100x",
    "fresh_4096_run_authorized",
    "disposition",
}
RUN_KEYS = {
    "key",
    "frame",
    "resolution",
    "samples",
    "sample_ranges",
    "baseline_s",
    "product_s",
    "measured_speedup_x",
    "benchmark_audit",
    "product_gate",
    "accepted",
    "repaired",
    "cache_used",
    "trial_count",
    "variance_estimate",
    "receipt",
    "local_artifacts",
}
GATE_KEYS = {
    "passed",
    "global_agreement",
    "worst_regional_agreement",
    "worst_microtile_agreement",
}
LOCAL_KEYS = {"root", "unit_relative_path", "files"}
MANIFEST_KEYS = {
    "artifact",
    "artifact_verified",
    "billing_eligible",
    "binding_sha256",
    "evidence",
    "execution_identity_revalidation",
    "kind",
    "phase",
    "pins",
    "preview_only",
    "production_ready",
    "render",
    "scene",
    "schema_version",
    "unit_id",
}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _resolve_path(raw: Any, location: str) -> Path:
    value = common._string(raw, location)
    if "\x00" in value:
        raise common._error(location, "contains a NUL byte")
    path = Path(value)
    return path if path.is_absolute() else REPO / path


def _descriptor(value: Any, location: str) -> dict[str, Any]:
    row = common._object(value, location)
    common._exact_keys(row, DESCRIPTOR_KEYS, location)
    common._string(row["path"], f"{location}.path")
    common._integer(row["bytes"], f"{location}.bytes", minimum=1)
    common._sha256(row["sha256"], f"{location}.sha256")
    return row


def _file_descriptor(value: Any, location: str) -> dict[str, Any]:
    row = common._object(value, location)
    common._exact_keys(row, {"bytes", "sha256"}, location)
    common._integer(row["bytes"], f"{location}.bytes", minimum=1)
    common._sha256(row["sha256"], f"{location}.sha256")
    return row


def _semantic_digest(proof: dict[str, Any]) -> str:
    normalized = copy.deepcopy(proof)
    for key in ("archive", "original_scene", "derivative"):
        normalized["source"][key]["path"] = "<relocatable-source-artifact>"
    for run in normalized["runs"]:
        run["receipt"]["path"] = "<relocatable-receipt>"
        run["local_artifacts"]["root"] = "<relocatable-artifact-root>"
    data = (
        json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    return _digest(data)


def _gate(value: Any, location: str) -> dict[str, Any]:
    gate = common._object(value, location)
    common._exact_keys(gate, GATE_KEYS, location)
    common._equal(gate["passed"], True, f"{location}.passed")
    thresholds = (0.9, 0.85, 0.7)
    for key, threshold in zip(
        (
            "global_agreement",
            "worst_regional_agreement",
            "worst_microtile_agreement",
        ),
        thresholds,
        strict=True,
    ):
        score = common._number(gate[key], f"{location}.{key}", minimum=0, maximum=1)
        if score < threshold:
            raise common._error(f"{location}.{key}", "unexpectedly failed its gate")
    return gate


def _validate_structure(proof: dict[str, Any]) -> None:
    common._reject_nonfinite_tree(proof)
    common._exact_keys(proof, PROOF_KEYS, "proof")
    common._equal(proof["schema_version"], 2, "proof.schema_version")
    common._equal(proof["kind"], PROOF_KIND, "proof.kind")
    common._string(proof["created_at_utc"], "proof.created_at_utc")
    common._string(proof["claim_scope"], "proof.claim_scope")

    source = common._object(proof["source"], "proof.source")
    common._exact_keys(source, SOURCE_KEYS, "proof.source")
    for key in ("archive", "original_scene", "derivative", "builder"):
        _descriptor(source[key], f"proof.source.{key}")

    common._exact_keys(
        common._object(proof["derivative_audit"], "proof.derivative_audit"),
        set(DERIVATIVE_AUDIT),
        "proof.derivative_audit",
    )
    common._exact_keys(
        common._object(proof["quality_contract"], "proof.quality_contract"),
        set(QUALITY_CONTRACT),
        "proof.quality_contract",
    )
    protocol = common._object(proof["protocol"], "proof.protocol")
    common._exact_keys(protocol, PROTOCOL_KEYS, "proof.protocol")
    pins = common._object(protocol["pins"], "proof.protocol.pins")
    common._exact_keys(pins, set(PINS), "proof.protocol.pins")
    for key in pins:
        common._sha256(pins[key], f"proof.protocol.pins.{key}")

    common._exact_keys(
        common._object(proof["deformation_change"], "proof.deformation_change"),
        CHANGE_KEYS,
        "proof.deformation_change",
    )
    common._exact_keys(
        common._object(proof["slope_screen"], "proof.slope_screen"),
        SLOPE_KEYS,
        "proof.slope_screen",
    )
    limitations = common._array(proof["limitations"], "proof.limitations")
    if len(limitations) != 4:
        raise common._error("proof.limitations", "must contain exactly four limitations")
    for index, item in enumerate(limitations):
        common._string(item, f"proof.limitations[{index}]")

    runs = common._array(proof["runs"], "proof.runs")
    if len(runs) != len(RUN_ORDER):
        raise common._error("proof.runs", "must contain exactly five runs")
    for index, raw in enumerate(runs):
        location = f"proof.runs[{index}]"
        run = common._object(raw, location)
        common._exact_keys(run, RUN_KEYS, location)
        common._exact_keys(
            common._object(run["samples"], f"{location}.samples"),
            {"draft", "verify", "baseline"},
            f"{location}.samples",
        )
        common._ranges(run["sample_ranges"], f"{location}.sample_ranges")
        _gate(run["benchmark_audit"], f"{location}.benchmark_audit")
        _gate(run["product_gate"], f"{location}.product_gate")
        _descriptor(run["receipt"], f"{location}.receipt")
        local = common._object(run["local_artifacts"], f"{location}.local_artifacts")
        common._exact_keys(local, LOCAL_KEYS, f"{location}.local_artifacts")
        common._string(local["root"], f"{location}.local_artifacts.root")
        common._relative_path(
            local["unit_relative_path"],
            f"{location}.local_artifacts.unit_relative_path",
        )
        files = common._object(local["files"], f"{location}.local_artifacts.files")
        common._exact_keys(files, FILE_NAMES, f"{location}.local_artifacts.files")
        for name, descriptor in files.items():
            _file_descriptor(descriptor, f"{location}.local_artifacts.files.{name}")


def _validate_semantics(proof: dict[str, Any]) -> None:
    _validate_structure(proof)
    constants = {
        "status": "pruned_negative_screen",
        "evidence": "measured_calibration_plus_cross_run_linear_projection",
        "receipt_trust": "local_unattested",
        "preview_only": True,
        "production_ready": False,
    }
    for key, expected in constants.items():
        common._equal(proof[key], expected, f"proof.{key}")

    source = proof["source"]
    common._equal(source["url"], SOURCE_URL, "proof.source.url")
    for key, expected in SOURCE_DESCRIPTORS.items():
        for field, wanted in expected.items():
            common._equal(source[key][field], wanted, f"proof.source.{key}.{field}")
    common._equal(proof["derivative_audit"], DERIVATIVE_AUDIT, "proof.derivative_audit")
    common._equal(proof["quality_contract"], QUALITY_CONTRACT, "proof.quality_contract")

    protocol = proof["protocol"]
    protocol_constants = {
        "run_count": 5,
        "run_order": RUN_ORDER,
        "timing_scope": "resident_steady_state_after_uncharged_warmup",
        "timing_statistics": "one fixed-order trial per run; no variance estimate",
        "cold_start_included": False,
        "cache_used": False,
        "renderer": "Blender 4.2.1 LTS / Cycles",
        "device": "GPU/METAL",
        "enabled_device_name": "Apple M3 Ultra (GPU - 60 cores)",
        "manifest_schema_version": 2,
        "audit_schema_version": 2,
        "verification_schema_version": 2,
        "pins": PINS,
        "projection_label": (
            "cross_run_linear_extrapolation_from_measured_256_and_512_spp_"
            "baselines_not_a_measured_speedup"
        ),
    }
    for key, expected in protocol_constants.items():
        common._equal(protocol[key], expected, f"proof.protocol.{key}")

    facts: dict[str, dict[str, Any]] = {}
    receipt_hashes: set[str] = set()
    artifact_roots: set[str] = set()
    for index, run in enumerate(proof["runs"]):
        location = f"proof.runs[{index}]"
        key = common._string(run["key"], f"{location}.key")
        common._equal(key, RUN_ORDER[index], f"{location}.key")
        if key in facts:
            raise common._error(f"{location}.key", "duplicate run key")
        frame, resolution, draft, verify, baseline = RUN_CONFIGS[key]
        common._equal(run["frame"], frame, f"{location}.frame")
        common._equal(run["resolution"], resolution, f"{location}.resolution")
        expected_samples = {"draft": draft, "verify": verify, "baseline": baseline}
        common._equal(run["samples"], expected_samples, f"{location}.samples")
        expected_ranges = {
            "draft": [0, draft],
            "verify": [draft, draft + verify],
            "baseline": [draft + verify, draft + verify + baseline],
        }
        common._equal(run["sample_ranges"], expected_ranges, f"{location}.sample_ranges")
        baseline_s = common._number(run["baseline_s"], f"{location}.baseline_s", minimum=0)
        product_s = common._number(run["product_s"], f"{location}.product_s", minimum=0)
        speedup = round(baseline_s / product_s, 6)
        common._equal(speedup, run["measured_speedup_x"], f"{location}.measured_speedup_x")
        for field, expected in {
            "accepted": True,
            "repaired": False,
            "cache_used": False,
            "trial_count": 1,
            "variance_estimate": None,
        }.items():
            common._equal(run[field], expected, f"{location}.{field}")
        receipt_hash = run["receipt"]["sha256"]
        if receipt_hash in receipt_hashes:
            raise common._error(f"{location}.receipt.sha256", "duplicate receipt hash")
        receipt_hashes.add(receipt_hash)
        root = run["local_artifacts"]["root"]
        if root in artifact_roots:
            raise common._error(f"{location}.local_artifacts.root", "duplicate artifact root")
        artifact_roots.add(root)
        facts[key] = run

    change = proof["deformation_change"]
    expected_change = {
        "run_keys": ["f6_512_r128_d2_v2", "f7_512_r128_d2_v2"],
        "artifact": "draft.png",
        "resolution": [512, 288],
        "global_agreement": 0.9424669674365695,
        "worst_regional_agreement": 0.563992289624183,
        "worst_microtile_agreement": 0.563992289624183,
        "nonblack_test": (
            "at_least_one_rgb_channel_greater_than_8_in_8_bit_display_rgb"
        ),
        "frame_6_nonblack_fraction": 0.2665812174479167,
        "frame_7_nonblack_fraction": 0.2886420355902778,
        "meaningful_full_frame_change": True,
    }
    common._equal(change, expected_change, "proof.deformation_change")
    if change["global_agreement"] >= 0.95:
        raise common._error("proof.deformation_change", "does not show a meaningful change")

    slope = proof["slope_screen"]
    common._equal(
        slope["method"],
        "two_point_linear_baseline_slope_across_separate_f7_runs",
        "proof.slope_screen.method",
    )
    points = common._array(slope["baseline_points"], "proof.slope_screen.baseline_points")
    if len(points) != 2:
        raise common._error("proof.slope_screen.baseline_points", "must contain two points")
    expected_point_keys = ["f7_1080_r256_d1_v1", "f7_1080_r512_d1_v1"]
    for index, point in enumerate(points):
        location = f"proof.slope_screen.baseline_points[{index}]"
        common._exact_keys(
            common._object(point, location),
            {"run_key", "samples", "seconds"},
            location,
        )
        key = expected_point_keys[index]
        common._equal(point["run_key"], key, f"{location}.run_key")
        common._equal(point["samples"], facts[key]["samples"]["baseline"], f"{location}.samples")
        common._equal(point["seconds"], facts[key]["baseline_s"], f"{location}.seconds")
    sample_delta = points[1]["samples"] - points[0]["samples"]
    seconds_delta = points[1]["seconds"] - points[0]["seconds"]
    actual_slope = seconds_delta / sample_delta
    common._equal(
        round(actual_slope, 10),
        slope["slope_seconds_per_sample"],
        "proof.slope_screen.slope_seconds_per_sample",
    )
    intercept = points[0]["seconds"] - actual_slope * points[0]["samples"]
    common._equal(round(intercept, 6), slope["intercept_seconds"], "proof.slope_screen.intercept_seconds")
    common._equal(slope["target_samples"], 4096, "proof.slope_screen.target_samples")
    projection = intercept + actual_slope * slope["target_samples"]
    common._equal(round(projection, 6), slope["projected_baseline_s"], "proof.slope_screen.projected_baseline_s")
    product_key = "f7_1080_r256_d1_v1"
    common._equal(slope["product_run_key"], product_key, "proof.slope_screen.product_run_key")
    common._equal(slope["product_s"], facts[product_key]["product_s"], "proof.slope_screen.product_s")
    projected_x = round(projection / slope["product_s"], 6)
    common._equal(projected_x, slope["projected_speedup_x"], "proof.slope_screen.projected_speedup_x")
    if projected_x >= 50:
        raise common._error("proof.slope_screen", "a negative screen cannot be promoted to 50x")
    slope_constants = {
        "measured_speedup": False,
        "measured_4096_baseline": False,
        "meets_50x": False,
        "meets_100x": False,
        "fresh_4096_run_authorized": False,
        "disposition": "pruned_below_50x_projection",
    }
    for key, expected in slope_constants.items():
        common._equal(slope[key], expected, f"proof.slope_screen.{key}")


def _verify_file(
    path: Path,
    descriptor: dict[str, Any],
    location: str,
    *,
    maximum: int = common.MAX_ARTIFACT_BYTES,
) -> bytes:
    data = common._stable_bytes(path, location, maximum=maximum)
    common._equal(len(data), descriptor["bytes"], f"{location}.bytes")
    common._equal(_digest(data), descriptor["sha256"], f"{location}.sha256")
    return data


def _verify_source_files(proof: dict[str, Any], require_local: bool) -> int:
    count = 0
    for key in ("archive", "original_scene", "derivative", "builder"):
        descriptor = proof["source"][key]
        path = _resolve_path(descriptor["path"], f"proof.source.{key}.path")
        if not path.exists():
            if require_local:
                raise common._error(f"local.source.{key}", "required local file is absent")
            continue
        _verify_file(path, descriptor, f"local.source.{key}")
        count += 1
    return count


def _validate_receipt(
    receipt: dict[str, Any], data: bytes, run: dict[str, Any], index: int
) -> None:
    location = f"local.runs[{index}].receipt"
    descriptor = run["receipt"]
    common._equal(len(data), descriptor["bytes"], f"{location}.bytes")
    common._equal(_digest(data), descriptor["sha256"], f"{location}.sha256")
    common._exact_keys(receipt, common.RECEIPT_KEYS, location)
    constants = {
        "schema_version": 1,
        "kind": "cx_local_cycles_spec_benchmark",
        "scene_sha256": DERIVATIVE_SHA256,
        "frame": run["frame"],
        "resolution": run["resolution"],
        "draft_samples": run["samples"]["draft"],
        "verify_samples": run["samples"]["verify"],
        "reference_samples": run["samples"]["baseline"],
        "sample_ranges": run["sample_ranges"],
        "sample_ranges_disjoint": True,
        "baseline_s": run["baseline_s"],
        "spec_s": run["product_s"],
        "speedup_x": run["measured_speedup_x"],
        "global_agreement": run["benchmark_audit"]["global_agreement"],
        "worst_tile_agreement": run["benchmark_audit"]["worst_regional_agreement"],
        "quality_gate": True,
        "cache_used": False,
        "trial_count": 1,
        "variance_estimate": None,
        "preview_only": True,
        "production_ready": False,
        "reference_used_for_product_decision": False,
        "device": "GPU/METAL",
        "pins": PINS,
    }
    for key, expected in constants.items():
        common._equal(receipt[key], expected, f"{location}.{key}")
    controller = receipt["controller_receipt"]
    common._equal(controller["total_product_time_s"], run["product_s"], f"{location}.controller_receipt.total_product_time_s")
    common._equal(controller["baseline_total_time_s"], run["baseline_s"], f"{location}.controller_receipt.baseline_total_time_s")
    common._equal(controller["speedup_vs_baseline"], run["measured_speedup_x"], f"{location}.controller_receipt.speedup_vs_baseline")
    for key, expected in {
        "accepted_units": 1,
        "repaired_units": 0,
        "quality_gate": True,
        "repair_cost_s": 0.0,
    }.items():
        common._equal(controller[key], expected, f"{location}.controller_receipt.{key}")
    costs = sum(
        controller[key]
        for key in ("draft_cost_s", "verify_cost_s", "overhead_cost_s", "repair_cost_s")
    )
    if abs(round(costs, 6) - run["product_s"]) > 0.0000011:
        raise common._error(
            f"{location}.controller_receipt.costs",
            "rounded components differ from the wire total by more than one microsecond",
        )

    audit = receipt["benchmark_audit"]
    common._exact_keys(audit, common.AUDIT_KEYS, f"{location}.benchmark_audit")
    audit_constants = {
        "schema_version": 2,
        "kind": "cx_cycles_preview_benchmark_audit",
        "passed": True,
        "measurement_only": True,
        "product_decision_used_reference": False,
        "metric": QUALITY_CONTRACT["metric"],
        "global_min": 0.9,
        "worst_tile_min": 0.85,
        "worst_microtile_min": 0.7,
        "sample_ranges": run["sample_ranges"],
        "sample_ranges_disjoint": True,
        "global_agreement": run["benchmark_audit"]["global_agreement"],
        "worst_tile_agreement": run["benchmark_audit"]["worst_regional_agreement"],
        "worst_microtile_agreement": run["benchmark_audit"]["worst_microtile_agreement"],
    }
    for key, expected in audit_constants.items():
        common._equal(audit[key], expected, f"{location}.benchmark_audit.{key}")
    output = receipt["outputs"]
    if len(output) != 1:
        raise common._error(f"{location}.outputs", "must contain one selected artifact")
    common._equal(output[0]["schema_version"], 2, f"{location}.outputs[0].schema_version")
    expected_manifest = str(PurePosixPath(run["local_artifacts"]["unit_relative_path"]) / "draft-manifest.json")
    common._equal(output[0]["manifest_path"], expected_manifest, f"{location}.outputs[0].manifest_path")
    identity = receipt["worker_renderer_identity"]
    common._equal(identity["blender_version"], "4.2.1 LTS", f"{location}.worker_renderer_identity.blender_version")
    common._equal(identity["device"], "GPU/METAL", f"{location}.worker_renderer_identity.device")
    common._equal(identity["enabled_device_names"], ["Apple M3 Ultra (GPU - 60 cores)"], f"{location}.worker_renderer_identity.enabled_device_names")
    common._equal(identity["candidate_profile"]["name"], "native", f"{location}.worker_renderer_identity.candidate_profile.name")


def _decode_png(data: bytes, resolution: list[int], location: str) -> Any:
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:  # pragma: no cover - local dependency boundary
        raise common._error(location, "Pillow is required for local image audits") from exc
    try:
        source = Image.open(BytesIO(data))
        if source.format != "PNG" or list(source.size) != resolution:
            raise common._error(location, "PNG shape or format mismatch")
        image = source.convert("RGB")
        image.load()
        source.close()
        return image
    except (OSError, UnidentifiedImageError) as exc:
        raise common._error(location, f"cannot decode PNG: {exc}") from exc


def _agreement(
    left_bytes: bytes,
    right_bytes: bytes,
    resolution: list[int],
    location: str,
    *,
    rounded: bool,
) -> tuple[float, float, float]:
    try:
        from PIL import ImageChops, ImageStat
    except ImportError as exc:  # pragma: no cover - local dependency boundary
        raise common._error(location, "Pillow is required for local image audits") from exc
    left = _decode_png(left_bytes, resolution, f"{location}.left")
    right = _decode_png(right_bytes, resolution, f"{location}.right")
    difference = ImageChops.difference(left, right)
    width, height = resolution

    def score(image: Any) -> float:
        means = ImageStat.Stat(image).mean
        return min(1.0, max(0.0, 1.0 - sum(means) / (len(means) * 255.0)))

    def minimum(columns: int, rows: int) -> float:
        return min(
            score(
                difference.crop(
                    (
                        column * width // columns,
                        row * height // rows,
                        (column + 1) * width // columns,
                        (row + 1) * height // rows,
                    )
                )
            )
            for row in range(rows)
            for column in range(columns)
        )

    result = (
        score(difference),
        minimum(16, 9),
        minimum(max(1, width // 32), max(1, height // 32)),
    )
    if rounded:
        return tuple(round(value, 9) for value in result)  # type: ignore[return-value]
    return result


def _validate_manifest(
    manifest: dict[str, Any], run: dict[str, Any], phase: str, index: int
) -> None:
    location = f"local.runs[{index}].{phase}-manifest.json"
    common._exact_keys(manifest, MANIFEST_KEYS, location)
    common._equal(manifest["schema_version"], 2, f"{location}.schema_version")
    common._equal(manifest["kind"], "cx_cycles_preview_manifest", f"{location}.kind")
    common._equal(manifest["phase"], phase, f"{location}.phase")
    common._equal(manifest["scene"]["sha256"], DERIVATIVE_SHA256, f"{location}.scene.sha256")
    render = manifest["render"]
    samples = run["samples"]["baseline" if phase == "baseline" else "draft"]
    offset = run["sample_ranges"]["baseline" if phase == "baseline" else "draft"][0]
    for key, expected in {
        "engine": "CYCLES",
        "device": "GPU/METAL",
        "frame": run["frame"],
        "width": run["resolution"][0],
        "height": run["resolution"][1],
        "samples": samples,
        "sample_offset": offset,
        "png_compression": 0,
    }.items():
        common._equal(render[key], expected, f"{location}.render.{key}")
    common._equal(manifest["pins"]["backend_sha256"], PINS["backend_sha256"], f"{location}.pins.backend_sha256")


def _verify_run_artifacts(
    run: dict[str, Any], receipt: dict[str, Any] | None, index: int, require_local: bool
) -> dict[str, bytes] | None:
    local = run["local_artifacts"]
    root = Path(local["root"])
    location = f"local.runs[{index}]"
    if not root.exists():
        if require_local:
            raise common._error(location, "required local artifact root is absent")
        return None
    if root.is_symlink() or not root.is_dir():
        raise common._error(location, "artifact root must be a non-symlink directory")
    unit = PurePosixPath(local["unit_relative_path"])
    contents: dict[str, bytes] = {}
    parsed: dict[str, dict[str, Any]] = {}
    for name in sorted(FILE_NAMES):
        path = common._contained_file(root, str(unit / name), f"{location}.{name}.path")
        data = _verify_file(path, local["files"][name], f"{location}.{name}")
        contents[name] = data
        if name.endswith(".json"):
            parsed[name] = common._strict_json_bytes(data, f"{location}.{name}")
    if receipt is None:
        if require_local:
            raise common._error(location, "artifacts require their raw receipt")
        return None

    for phase in ("draft", "baseline"):
        _validate_manifest(parsed[f"{phase}-manifest.json"], run, phase, index)
        manifest = parsed[f"{phase}-manifest.json"]
        png_name = f"{phase}.png"
        expected_path = str(unit / png_name)
        common._equal(manifest["artifact"]["path"], expected_path, f"{location}.{phase}-manifest.json.artifact.path")
        common._equal(manifest["artifact"]["sha256"], local["files"][png_name]["sha256"], f"{location}.{phase}-manifest.json.artifact.sha256")

    verification = parsed["verification-manifest.json"]
    common._exact_keys(verification, common.VERIFICATION_KEYS, f"{location}.verification-manifest.json")
    common._equal(verification["schema_version"], 2, f"{location}.verification-manifest.json.schema_version")
    common._equal(verification["accepted"], True, f"{location}.verification-manifest.json.accepted")
    common._equal(verification["repair_plan"], None, f"{location}.verification-manifest.json.repair_plan")
    common._equal(verification["verify_artifact"]["sha256"], local["files"]["verify.png"]["sha256"], f"{location}.verification-manifest.json.verify_artifact.sha256")
    gate_mapping = {
        "global_agreement": "global_agreement",
        "worst_tile_agreement": "worst_regional_agreement",
        "worst_microtile_agreement": "worst_microtile_agreement",
    }
    for manifest_key, proof_key in gate_mapping.items():
        common._equal(verification[manifest_key], run["product_gate"][proof_key], f"{location}.verification-manifest.json.{manifest_key}")

    audit = parsed["benchmark-audit.json"]
    common._exact_keys(audit, common.AUDIT_KEYS, f"{location}.benchmark-audit.json")
    common._equal(audit, receipt["benchmark_audit"], f"{location}.benchmark-audit.json")
    common._equal(audit["schema_version"], 2, f"{location}.benchmark-audit.json.schema_version")
    common._equal(audit["candidate"]["artifact_sha256"], local["files"]["draft.png"]["sha256"], f"{location}.benchmark-audit.json.candidate.artifact_sha256")
    common._equal(audit["baseline"]["artifact_sha256"], local["files"]["baseline.png"]["sha256"], f"{location}.benchmark-audit.json.baseline.artifact_sha256")

    audit_actual = _agreement(
        contents["draft.png"],
        contents["baseline.png"],
        run["resolution"],
        f"{location}.benchmark_audit",
        rounded=True,
    )
    audit_expected = (
        run["benchmark_audit"]["global_agreement"],
        run["benchmark_audit"]["worst_regional_agreement"],
        run["benchmark_audit"]["worst_microtile_agreement"],
    )
    common._equal(audit_actual, audit_expected, f"{location}.benchmark_audit")
    gate_actual = _agreement(
        contents["draft.png"],
        contents["verify.png"],
        run["resolution"],
        f"{location}.product_gate",
        rounded=True,
    )
    gate_expected = (
        run["product_gate"]["global_agreement"],
        run["product_gate"]["worst_regional_agreement"],
        run["product_gate"]["worst_microtile_agreement"],
    )
    common._equal(gate_actual, gate_expected, f"{location}.product_gate")
    return contents


def _nonblack_fraction(data: bytes, resolution: list[int], location: str) -> float:
    image = _decode_png(data, resolution, location)
    pixels = image.tobytes()
    count = sum(
        1
        for offset in range(0, len(pixels), 3)
        if max(pixels[offset : offset + 3]) > 8
    )
    return count / (resolution[0] * resolution[1])


def verify(
    proof_path: Path = DEFAULT_PROOF, *, require_local_artifacts: bool = False
) -> dict[str, Any]:
    proof, proof_bytes = common._read_json(proof_path, "proof")
    _validate_semantics(proof)
    semantic_sha = _semantic_digest(proof)
    common._equal(semantic_sha, EXPECTED_SEMANTIC_SHA256, "proof.semantic_sha256")

    source_count = _verify_source_files(proof, require_local_artifacts)
    receipt_count = 0
    artifact_count = 0
    artifact_files_count = 0
    images: dict[str, dict[str, bytes]] = {}
    for index, run in enumerate(proof["runs"]):
        receipt_path = _resolve_path(run["receipt"]["path"], f"proof.runs[{index}].receipt.path")
        receipt: dict[str, Any] | None = None
        if receipt_path.exists():
            receipt, receipt_bytes = common._read_json(receipt_path, f"local.runs[{index}].receipt")
            _validate_receipt(receipt, receipt_bytes, run, index)
            receipt_count += 1
        elif require_local_artifacts:
            raise common._error(f"local.runs[{index}].receipt", "required local receipt is absent")
        contents = _verify_run_artifacts(
            run, receipt, index, require_local_artifacts
        )
        if contents is not None:
            artifact_count += 1
            artifact_files_count += len(FILE_NAMES)
            images[run["key"]] = contents

    change_recomputed = False
    change_keys = proof["deformation_change"]["run_keys"]
    if all(key in images for key in change_keys):
        left = images[change_keys[0]]["draft.png"]
        right = images[change_keys[1]]["draft.png"]
        actual = _agreement(
            left,
            right,
            [512, 288],
            "local.deformation_change",
            rounded=False,
        )
        expected = (
            proof["deformation_change"]["global_agreement"],
            proof["deformation_change"]["worst_regional_agreement"],
            proof["deformation_change"]["worst_microtile_agreement"],
        )
        common._equal(actual, expected, "local.deformation_change.agreement")
        fractions = (
            _nonblack_fraction(left, [512, 288], "local.deformation_change.frame_6"),
            _nonblack_fraction(right, [512, 288], "local.deformation_change.frame_7"),
        )
        common._equal(
            fractions,
            (
                proof["deformation_change"]["frame_6_nonblack_fraction"],
                proof["deformation_change"]["frame_7_nonblack_fraction"],
            ),
            "local.deformation_change.nonblack_fractions",
        )
        change_recomputed = True

    local_complete = (
        source_count == 4
        and receipt_count == len(RUN_ORDER)
        and artifact_count == len(RUN_ORDER)
        and artifact_files_count == len(RUN_ORDER) * len(FILE_NAMES)
        and change_recomputed
    )
    if require_local_artifacts and not local_complete:
        raise common._error("local", "required local artifact verification is incomplete")
    return {
        "schema_version": 2,
        "kind": RESULT_KIND,
        "valid": True,
        "status": "pruned_negative_screen",
        "proof_sha256": _digest(proof_bytes),
        "semantic_sha256": semantic_sha,
        "measured_calibration_runs": 5,
        "projected_4096_baseline_s": 29.886616,
        "projected_speedup_x": 35.69766,
        "projection_is_measured": False,
        "meets_50x": False,
        "fresh_4096_run_authorized": False,
        "source_files_verified": source_count,
        "local_receipts_verified": receipt_count,
        "local_artifact_runs_verified": artifact_count,
        "local_artifact_files_verified": artifact_files_count,
        "deformation_change_recomputed": change_recomputed,
        "local_artifacts_verified": local_complete,
        "production_ready": False,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proof", type=Path, default=DEFAULT_PROOF)
    parser.add_argument("--require-local-artifacts", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = verify(args.proof, require_local_artifacts=args.require_local_artifacts)
    except Exception as exc:  # noqa: BLE001 - one fail-closed CLI envelope
        print(
            json.dumps(
                {
                    "schema_version": 2,
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
