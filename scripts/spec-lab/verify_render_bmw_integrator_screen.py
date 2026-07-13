#!/usr/bin/env python3
"""Fail-closed verifier for the bounded BMW27 integrator-profile screen.

The committed proof records a negative result.  Its eight 256-spp screens are
measured, while every ratio against the retained 4096-spp baseline is explicitly
a cross-session arithmetic projection.  Local cache roots are relocatable and
optional; when present, the verifier hashes every raw receipt, manifest, and PNG
and recomputes every retained-reference image audit.
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
    / "proof/performance/apple-metal-render-bmw-integrator-screen-2026-07-12.json"
)
PROOF_KIND = "cx_local_cycles_bmw_integrator_screen"
RESULT_KIND = "cx_render_bmw_integrator_screen_verification"
EXPECTED_SEMANTIC_SHA256 = (
    "7944b563ecdfa4f80a4b283d4c01ca97590ce1a6d2380143caa22547f5d6ca42"
)
SCENE_SHA256 = "172a6b1c6f25acf59d17cb2682866a6b7a33718b92ad15f4073d55d16ff3af74"
HISTORICAL_RECEIPT_SHA256 = (
    "e88be50b35fd99dd1cf9ceb6b6df7f87c6601c6539a2c8df85866eb40e7a3db3"
)
HISTORICAL_BASELINE_S = 59.313663
PROJECTION_LABEL = (
    "cross_session_projection_historical_4096_baseline_divided_by_"
    "screen_product_time_not_a_measured_speedup"
)
ARM_ORDER = [
    "native",
    "cap16_v1",
    "cap12_v1",
    "cap8_v1",
    "cap8_lighttree_v1",
    "cap8_adaptive_v1",
    "cap8_both_v1",
    "cap8_both_relaxed_v1",
]
GENERATION_ARMS = {
    "generation_1_bounce_caps": ["cap16_v1", "cap12_v1", "cap8_v1"],
    "generation_2_sampling_and_light_tree": [
        "native",
        "cap8_lighttree_v1",
        "cap8_adaptive_v1",
        "cap8_both_v1",
        "cap8_both_relaxed_v1",
    ],
}
GENERATION_PINS = {
    "generation_1_bounce_caps": {
        "backend_sha256": "8d544c42f156e4d0ddcabc156595e4032cc67bc13f3e26bd0c905b3a4ba9599e",
        "benchmark_harness_sha256": "6e89d51ecc99c2b56e77acb719fdd5baf6d9b2d0d55bdacfb904f0b08afda2e7",
    },
    "generation_2_sampling_and_light_tree": {
        "backend_sha256": "22f938f14c10ded3a3296ef98afaab5f2bdeaba6a33537d60b412025df6542f4",
        "benchmark_harness_sha256": "1f4eb40d0d3ef0082296357a456f312253e18a790a6e4d385335333c01e48b1e",
    },
}
SHARED_PINS = {
    "blender_sha256": "b5737da97b0e164cc18e227be115c4ea2791d11e9577f7e302c73f4871f9249c",
    "controller_adapter_sha256": "fbd72047b0497a6ce886c22bb86ba4a4b374a5ebd05b86cc1eb4c2d91afd534e",
    "controller_core_sha256": "c729c8b9e5d231f9c6566d1bc8c492af2ecad4fc2aade7cf2a188f35428d9a11",
    "render_preview_driver_sha256": "5d05da0214b9dab5446910a303691b2a5b342a6658a22e1d4aa0faf07abc1e6b",
}
NATIVE_INTEGRATOR = {
    "diffuse_bounces": 16,
    "glossy_bounces": 16,
    "max_bounces": 32,
    "transmission_bounces": 32,
}
REFERENCE_SAMPLING = {
    "adaptive_min_samples": 0,
    "adaptive_threshold": 0.009999999776482582,
    "use_adaptive_sampling": False,
    "use_light_tree": False,
}
ACTUAL_SAMPLING = {
    "native": REFERENCE_SAMPLING,
    "cap8_lighttree_v1": {**REFERENCE_SAMPLING, "use_light_tree": True},
    "cap8_adaptive_v1": {
        "adaptive_min_samples": 8,
        "adaptive_threshold": 0.009999999776482582,
        "use_adaptive_sampling": True,
        "use_light_tree": False,
    },
    "cap8_both_v1": {
        "adaptive_min_samples": 8,
        "adaptive_threshold": 0.009999999776482582,
        "use_adaptive_sampling": True,
        "use_light_tree": True,
    },
    "cap8_both_relaxed_v1": {
        "adaptive_min_samples": 8,
        "adaptive_threshold": 0.019999999552965164,
        "use_adaptive_sampling": True,
        "use_light_tree": True,
    },
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
    "scene",
    "quality_contract",
    "protocol",
    "summary",
    "arms",
    "limitations",
}
SCENE_KEYS = {
    "key",
    "family",
    "source_url",
    "archive_sha256",
    "scene_sha256",
    "frame",
    "resolution",
    "renderer",
    "device",
    "enabled_device_name",
}
QUALITY_KEYS = {
    "metric",
    "global_min",
    "worst_regional_min",
    "worst_microtile_min",
    "microtile_nominal_edge_pixels",
    "reference_used_for_product_decision",
}
PROTOCOL_KEYS = {
    "arm_count",
    "arm_order",
    "screen_samples",
    "sample_ranges",
    "sample_ranges_disjoint",
    "adaptive_sample_semantics",
    "timing_scope",
    "timing_statistics",
    "cold_start_included",
    "cache_used",
    "generations",
    "shared_pins",
    "historical_4096_reference",
    "projection_label",
}
GENERATION_KEYS = {
    "id",
    "arms",
    "backend_sha256",
    "benchmark_harness_sha256",
}
HISTORICAL_KEYS = {
    "receipt_path",
    "receipt_bytes",
    "receipt_sha256",
    "baseline_s",
    "reference_samples",
    "sample_range",
    "artifact_root",
    "unit_relative_path",
    "baseline_manifest",
    "baseline_png",
}
SUMMARY_KEYS = {
    "arms_screened",
    "arms_passing_product_gate",
    "arms_passing_retained_4096_audit",
    "measured_cross_session_speedup_claims",
    "arms_meeting_50x_measured",
    "arms_meeting_50x_projection",
    "arms_meeting_100x_projection",
    "best_arm",
    "best_product_s",
    "best_cross_session_projection_x",
    "native_cross_session_projection_x",
    "native_cross_session_projection_x_6dp",
    "fresh_4096_run_authorized",
    "disposition",
}
ARM_KEYS = {
    "key",
    "generation",
    "candidate_profile",
    "receipt",
    "screen",
    "product_gate",
    "retained_4096_audit",
    "projection",
    "local_artifacts",
    "decision",
}
SCREEN_KEYS = {
    "baseline_256_s",
    "product_s",
    "measured_256_speedup_x",
    "benchmark_global_agreement",
    "benchmark_worst_regional_agreement",
    "benchmark_worst_microtile_agreement",
    "accepted",
    "repaired",
    "trial_count",
    "variance_estimate",
}
GATE_KEYS = {
    "passed",
    "global_agreement",
    "worst_regional_agreement",
    "worst_microtile_agreement",
}
RETAINED_KEYS = GATE_KEYS | {"recomputed_post_hoc"}
PROJECTION_KEYS = {
    "historical_baseline_s",
    "projected_x",
    "projected_x_6dp",
    "measured",
    "meets_50x",
    "meets_100x",
}
DESCRIPTOR_KEYS = {"bytes", "sha256"}
RECEIPT_DESCRIPTOR_KEYS = {"path", "bytes", "sha256"}
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
RENDER_KEYS = {
    "device",
    "engine",
    "frame",
    "height",
    "integrator_policy",
    "pixel_filter",
    "png_compression",
    "sample_offset",
    "samples",
    "seed",
    "width",
    "worker_renderer_identity",
}
MANIFEST_PIN_KEYS = {
    "blender_sha256",
    "backend_sha256",
    "child_script_sha256",
    "controller_core_sha256",
    "controller_adapter_sha256",
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
    common._integer(row["bytes"], f"{location}.bytes", minimum=1)
    common._sha256(row["sha256"], f"{location}.sha256")
    return row


def _semantic_digest(proof: dict[str, Any]) -> str:
    normalized = copy.deepcopy(proof)
    history = normalized["protocol"]["historical_4096_reference"]
    history["artifact_root"] = "<relocatable-historical-artifact-root>"
    for arm in normalized["arms"]:
        arm["receipt"]["path"] = "<relocatable-local-receipt>"
        arm["local_artifacts"]["root"] = "<relocatable-local-artifact-root>"
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


def _validate_structure(proof: dict[str, Any]) -> None:
    common._exact_keys(proof, PROOF_KEYS, "proof")
    common._equal(proof["schema_version"], 1, "proof.schema_version")
    common._equal(proof["kind"], PROOF_KIND, "proof.kind")
    common._string(proof["created_at_utc"], "proof.created_at_utc")
    common._string(proof["claim_scope"], "proof.claim_scope")

    scene = common._object(proof["scene"], "proof.scene")
    common._exact_keys(scene, SCENE_KEYS, "proof.scene")
    for key in SCENE_KEYS - {"frame", "resolution"}:
        common._string(scene[key], f"proof.scene.{key}")
    common._integer(scene["frame"], "proof.scene.frame", minimum=0)
    common._equal(scene["resolution"], [1920, 1080], "proof.scene.resolution")

    quality = common._object(proof["quality_contract"], "proof.quality_contract")
    common._exact_keys(quality, QUALITY_KEYS, "proof.quality_contract")
    for key in ("global_min", "worst_regional_min", "worst_microtile_min"):
        common._number(quality[key], f"proof.quality_contract.{key}", minimum=0.0, maximum=1.0)
    common._integer(
        quality["microtile_nominal_edge_pixels"],
        "proof.quality_contract.microtile_nominal_edge_pixels",
        minimum=1,
    )
    common._boolean(
        quality["reference_used_for_product_decision"],
        "proof.quality_contract.reference_used_for_product_decision",
    )

    protocol = common._object(proof["protocol"], "proof.protocol")
    common._exact_keys(protocol, PROTOCOL_KEYS, "proof.protocol")
    common._integer(protocol["arm_count"], "proof.protocol.arm_count", minimum=1)
    common._array(protocol["arm_order"], "proof.protocol.arm_order")
    for key in (
        "adaptive_sample_semantics",
        "timing_scope",
        "timing_statistics",
        "projection_label",
    ):
        common._string(protocol[key], f"proof.protocol.{key}")
    common._object(protocol["screen_samples"], "proof.protocol.screen_samples")
    common._object(protocol["sample_ranges"], "proof.protocol.sample_ranges")
    generations = common._array(protocol["generations"], "proof.protocol.generations")
    for index, raw in enumerate(generations):
        location = f"proof.protocol.generations[{index}]"
        generation = common._object(raw, location)
        common._exact_keys(generation, GENERATION_KEYS, location)
        common._string(generation["id"], f"{location}.id")
        common._array(generation["arms"], f"{location}.arms")
        common._sha256(generation["backend_sha256"], f"{location}.backend_sha256")
        common._sha256(
            generation["benchmark_harness_sha256"],
            f"{location}.benchmark_harness_sha256",
        )
    shared = common._object(protocol["shared_pins"], "proof.protocol.shared_pins")
    common._exact_keys(shared, set(SHARED_PINS), "proof.protocol.shared_pins")
    for key, value in shared.items():
        common._sha256(value, f"proof.protocol.shared_pins.{key}")
    history = common._object(
        protocol["historical_4096_reference"],
        "proof.protocol.historical_4096_reference",
    )
    common._exact_keys(history, HISTORICAL_KEYS, "proof.protocol.historical_4096_reference")
    common._string(history["receipt_path"], "proof.protocol.historical_4096_reference.receipt_path")
    common._integer(history["receipt_bytes"], "proof.protocol.historical_4096_reference.receipt_bytes", minimum=1)
    common._sha256(history["receipt_sha256"], "proof.protocol.historical_4096_reference.receipt_sha256")
    common._number(history["baseline_s"], "proof.protocol.historical_4096_reference.baseline_s", minimum=0.000001)
    common._integer(history["reference_samples"], "proof.protocol.historical_4096_reference.reference_samples", minimum=1)
    common._array(history["sample_range"], "proof.protocol.historical_4096_reference.sample_range")
    common._string(history["artifact_root"], "proof.protocol.historical_4096_reference.artifact_root")
    common._relative_path(history["unit_relative_path"], "proof.protocol.historical_4096_reference.unit_relative_path")
    _descriptor(history["baseline_manifest"], "proof.protocol.historical_4096_reference.baseline_manifest")
    _descriptor(history["baseline_png"], "proof.protocol.historical_4096_reference.baseline_png")

    summary = common._object(proof["summary"], "proof.summary")
    common._exact_keys(summary, SUMMARY_KEYS, "proof.summary")
    arms = common._array(proof["arms"], "proof.arms")
    for index, raw in enumerate(arms):
        location = f"proof.arms[{index}]"
        arm = common._object(raw, location)
        common._exact_keys(arm, ARM_KEYS, location)
        common._string(arm["key"], f"{location}.key")
        common._string(arm["generation"], f"{location}.generation")
        profile = common._object(arm["candidate_profile"], f"{location}.candidate_profile")
        profile_keys = (
            {"diffuse_bounces", "glossy_bounces", "max_bounces", "name", "transmission_bounces"}
            if arm["generation"] == "generation_1_bounce_caps"
            else {
                "adaptive_min_samples",
                "adaptive_threshold",
                "diffuse_bounces",
                "glossy_bounces",
                "max_bounces",
                "name",
                "transmission_bounces",
                "use_adaptive_sampling",
                "use_light_tree",
            }
        )
        common._exact_keys(profile, profile_keys, f"{location}.candidate_profile")
        receipt = common._object(arm["receipt"], f"{location}.receipt")
        common._exact_keys(receipt, RECEIPT_DESCRIPTOR_KEYS, f"{location}.receipt")
        common._string(receipt["path"], f"{location}.receipt.path")
        common._integer(receipt["bytes"], f"{location}.receipt.bytes", minimum=1)
        common._sha256(receipt["sha256"], f"{location}.receipt.sha256")
        screen = common._object(arm["screen"], f"{location}.screen")
        common._exact_keys(screen, SCREEN_KEYS, f"{location}.screen")
        for key in SCREEN_KEYS - {"accepted", "repaired", "trial_count", "variance_estimate"}:
            common._number(screen[key], f"{location}.screen.{key}", minimum=0.0)
        common._boolean(screen["accepted"], f"{location}.screen.accepted")
        common._boolean(screen["repaired"], f"{location}.screen.repaired")
        common._integer(screen["trial_count"], f"{location}.screen.trial_count", minimum=1)
        common._equal(screen["variance_estimate"], None, f"{location}.screen.variance_estimate")
        for field, expected_keys in (
            ("product_gate", GATE_KEYS),
            ("retained_4096_audit", RETAINED_KEYS),
        ):
            gate_location = f"{location}.{field}"
            gate = common._object(arm[field], gate_location)
            common._exact_keys(gate, expected_keys, gate_location)
            common._boolean(gate["passed"], f"{gate_location}.passed")
            for key in ("global_agreement", "worst_regional_agreement", "worst_microtile_agreement"):
                common._number(gate[key], f"{gate_location}.{key}", minimum=0.0, maximum=1.0)
            if field == "retained_4096_audit":
                common._boolean(gate["recomputed_post_hoc"], f"{gate_location}.recomputed_post_hoc")
        projection = common._object(arm["projection"], f"{location}.projection")
        common._exact_keys(projection, PROJECTION_KEYS, f"{location}.projection")
        for key in ("historical_baseline_s", "projected_x"):
            common._number(projection[key], f"{location}.projection.{key}", minimum=0.0)
        common._string(projection["projected_x_6dp"], f"{location}.projection.projected_x_6dp")
        for key in ("measured", "meets_50x", "meets_100x"):
            common._boolean(projection[key], f"{location}.projection.{key}")
        local = common._object(arm["local_artifacts"], f"{location}.local_artifacts")
        common._exact_keys(local, LOCAL_KEYS, f"{location}.local_artifacts")
        common._string(local["root"], f"{location}.local_artifacts.root")
        common._relative_path(local["unit_relative_path"], f"{location}.local_artifacts.unit_relative_path")
        files = common._object(local["files"], f"{location}.local_artifacts.files")
        common._exact_keys(files, FILE_NAMES, f"{location}.local_artifacts.files")
        for name in sorted(FILE_NAMES):
            _descriptor(files[name], f"{location}.local_artifacts.files.{name}")
        common._string(arm["decision"], f"{location}.decision")
    limitations = common._array(proof["limitations"], "proof.limitations")
    if len(limitations) < 6:
        raise common._error("proof.limitations", "must contain at least six caveats")
    for index, item in enumerate(limitations):
        common._string(item, f"proof.limitations[{index}]")


def _gate_passes(gate: dict[str, Any]) -> bool:
    return (
        gate["global_agreement"] >= common.GLOBAL_MIN
        and gate["worst_regional_agreement"] >= common.REGIONAL_MIN
        and gate["worst_microtile_agreement"] >= common.MICROTILE_MIN
    )


def _validate_semantics(proof: dict[str, Any]) -> None:
    constants = {
        "status": "pruned_negative_screen",
        "evidence": "measured_256_spp_screens_plus_cross_session_projection",
        "receipt_trust": "local_unattested",
        "preview_only": True,
        "production_ready": False,
    }
    for key, expected in constants.items():
        common._equal(proof[key], expected, f"proof.{key}")
    scene = proof["scene"]
    common._equal(scene["scene_sha256"], SCENE_SHA256, "proof.scene.scene_sha256")
    common._equal(scene["frame"], 1, "proof.scene.frame")
    common._equal(scene["device"], "GPU/METAL", "proof.scene.device")
    quality = proof["quality_contract"]
    common._equal(
        quality,
        {
            "metric": common.QUALITY_METRIC,
            "global_min": common.GLOBAL_MIN,
            "worst_regional_min": common.REGIONAL_MIN,
            "worst_microtile_min": common.MICROTILE_MIN,
            "microtile_nominal_edge_pixels": common.MICROTILE_EDGE,
            "reference_used_for_product_decision": False,
        },
        "proof.quality_contract",
    )
    protocol = proof["protocol"]
    common._equal(protocol["arm_count"], 8, "proof.protocol.arm_count")
    common._equal(protocol["arm_order"], ARM_ORDER, "proof.protocol.arm_order")
    common._equal(protocol["screen_samples"], {"draft": 32, "verify": 32, "baseline": 256}, "proof.protocol.screen_samples")
    common._equal(protocol["sample_ranges"], {"draft": [0, 32], "verify": [32, 64], "baseline": [64, 320]}, "proof.protocol.sample_ranges")
    common._equal(protocol["sample_ranges_disjoint"], True, "proof.protocol.sample_ranges_disjoint")
    common._equal(protocol["cold_start_included"], False, "proof.protocol.cold_start_included")
    common._equal(protocol["cache_used"], False, "proof.protocol.cache_used")
    common._equal(protocol["shared_pins"], SHARED_PINS, "proof.protocol.shared_pins")
    common._equal(protocol["projection_label"], PROJECTION_LABEL, "proof.protocol.projection_label")
    generations = protocol["generations"]
    common._equal([row["id"] for row in generations], list(GENERATION_ARMS), "proof.protocol.generations.ids")
    for row in generations:
        generation = row["id"]
        common._equal(row["arms"], GENERATION_ARMS[generation], f"proof.protocol.generations.{generation}.arms")
        for key, value in GENERATION_PINS[generation].items():
            common._equal(row[key], value, f"proof.protocol.generations.{generation}.{key}")
    history = protocol["historical_4096_reference"]
    common._equal(history["receipt_sha256"], HISTORICAL_RECEIPT_SHA256, "proof.protocol.historical_4096_reference.receipt_sha256")
    common._equal(history["receipt_bytes"], 7843, "proof.protocol.historical_4096_reference.receipt_bytes")
    common._equal(history["baseline_s"], HISTORICAL_BASELINE_S, "proof.protocol.historical_4096_reference.baseline_s")
    common._equal(history["reference_samples"], 4096, "proof.protocol.historical_4096_reference.reference_samples")
    common._equal(history["sample_range"], [64, 4160], "proof.protocol.historical_4096_reference.sample_range")

    arms = proof["arms"]
    common._equal([arm["key"] for arm in arms], ARM_ORDER, "proof.arms.order")
    projections: list[tuple[str, float, float]] = []
    for index, arm in enumerate(arms):
        location = f"proof.arms[{index}]"
        key = arm["key"]
        generation = arm["generation"]
        if key not in GENERATION_ARMS.get(generation, []):
            raise common._error(f"{location}.generation", "arm is assigned to the wrong code generation")
        common._equal(arm["candidate_profile"]["name"], key, f"{location}.candidate_profile.name")
        screen = arm["screen"]
        common._equal(screen["accepted"], True, f"{location}.screen.accepted")
        common._equal(screen["repaired"], False, f"{location}.screen.repaired")
        common._equal(screen["trial_count"], 1, f"{location}.screen.trial_count")
        expected_screen_ratio = round(screen["baseline_256_s"] / screen["product_s"], 6)
        common._equal(screen["measured_256_speedup_x"], expected_screen_ratio, f"{location}.screen.measured_256_speedup_x")
        for field in ("product_gate", "retained_4096_audit"):
            gate = arm[field]
            common._equal(gate["passed"], _gate_passes(gate), f"{location}.{field}.passed")
            if not gate["passed"]:
                raise common._error(f"{location}.{field}", "screen row unexpectedly failed its quality gate")
        common._equal(arm["retained_4096_audit"]["recomputed_post_hoc"], True, f"{location}.retained_4096_audit.recomputed_post_hoc")
        projection = arm["projection"]
        expected_projection = round(HISTORICAL_BASELINE_S / screen["product_s"], 6)
        common._equal(projection["historical_baseline_s"], HISTORICAL_BASELINE_S, f"{location}.projection.historical_baseline_s")
        common._equal(projection["projected_x"], expected_projection, f"{location}.projection.projected_x")
        common._equal(projection["projected_x_6dp"], f"{expected_projection:.6f}", f"{location}.projection.projected_x_6dp")
        common._equal(projection["measured"], False, f"{location}.projection.measured")
        common._equal(projection["meets_50x"], expected_projection >= 50.0, f"{location}.projection.meets_50x")
        common._equal(projection["meets_100x"], expected_projection >= 100.0, f"{location}.projection.meets_100x")
        projections.append((key, screen["product_s"], expected_projection))
    best = max(projections, key=lambda item: item[2])
    summary = proof["summary"]
    expected_summary = {
        "arms_screened": len(arms),
        "arms_passing_product_gate": sum(arm["product_gate"]["passed"] for arm in arms),
        "arms_passing_retained_4096_audit": sum(arm["retained_4096_audit"]["passed"] for arm in arms),
        "measured_cross_session_speedup_claims": sum(arm["projection"]["measured"] for arm in arms),
        "arms_meeting_50x_measured": 0,
        "arms_meeting_50x_projection": sum(arm["projection"]["meets_50x"] for arm in arms),
        "arms_meeting_100x_projection": sum(arm["projection"]["meets_100x"] for arm in arms),
        "best_arm": best[0],
        "best_product_s": best[1],
        "best_cross_session_projection_x": best[2],
        "native_cross_session_projection_x": arms[0]["projection"]["projected_x"],
        "native_cross_session_projection_x_6dp": arms[0]["projection"]["projected_x_6dp"],
        "fresh_4096_run_authorized": False,
        "disposition": "pruned_no_arm_reached_50x_projection",
    }
    common._equal(summary, expected_summary, "proof.summary")
    common._equal(best, ("cap8_v1", 1.472072, 40.292637), "proof.summary.best")


def _validate_receipt(
    receipt: dict[str, Any], receipt_bytes: bytes, arm: dict[str, Any], index: int
) -> None:
    location = f"local.arms[{index}].receipt"
    descriptor = arm["receipt"]
    common._equal(len(receipt_bytes), descriptor["bytes"], f"{location}.bytes")
    common._equal(_digest(receipt_bytes), descriptor["sha256"], f"{location}.sha256")
    common._exact_keys(receipt, common.RECEIPT_KEYS, location)
    screen = arm["screen"]
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
        "frame": 1,
        "resolution": [1920, 1080],
        "draft_samples": 32,
        "verify_samples": 32,
        "reference_samples": 256,
        "timing_scope": "resident_steady_state_after_uncharged_warmup",
        "timing_statistics": "fixed-order single trial; no variance estimate",
        "scene_sha256": SCENE_SHA256,
    }
    for key, expected in constants.items():
        common._equal(receipt[key], expected, f"{location}.{key}")
    common._equal(receipt["sample_ranges"], {"draft": [0, 32], "verify": [32, 64], "baseline": [64, 320]}, f"{location}.sample_ranges")
    common._equal(receipt["baseline_s"], screen["baseline_256_s"], f"{location}.baseline_s")
    common._equal(receipt["spec_s"], screen["product_s"], f"{location}.spec_s")
    common._equal(receipt["speedup_x"], screen["measured_256_speedup_x"], f"{location}.speedup_x")
    common._equal(receipt["meets_50x_preview_experiment"], False, f"{location}.meets_50x_preview_experiment")
    common._equal(receipt["meets_100x_preview_experiment"], False, f"{location}.meets_100x_preview_experiment")

    pins = common._object(receipt["pins"], f"{location}.pins")
    expected_pins = {**SHARED_PINS, **GENERATION_PINS[arm["generation"]]}
    common._exact_keys(pins, set(expected_pins), f"{location}.pins")
    common._equal(pins, expected_pins, f"{location}.pins")
    worker = common._object(receipt["worker_renderer_identity"], f"{location}.worker_renderer_identity")
    worker_keys = {
        "blender_build_hash",
        "blender_version",
        "candidate_profile",
        "dependency_count",
        "dependency_paths_sha256",
        "device",
        "enabled_device_names",
        "native_integrator",
        "png_compression",
    }
    if arm["generation"] == "generation_2_sampling_and_light_tree":
        worker_keys.add("reference_sampling")
    common._exact_keys(worker, worker_keys, f"{location}.worker_renderer_identity")
    common._equal(worker["candidate_profile"], arm["candidate_profile"], f"{location}.worker_renderer_identity.candidate_profile")
    common._equal(worker["native_integrator"], NATIVE_INTEGRATOR, f"{location}.worker_renderer_identity.native_integrator")
    common._equal(worker["device"], "GPU/METAL", f"{location}.worker_renderer_identity.device")
    common._equal(worker["enabled_device_names"], ["Apple M3 Ultra (GPU - 60 cores)"], f"{location}.worker_renderer_identity.enabled_device_names")
    common._equal(worker["png_compression"], 0, f"{location}.worker_renderer_identity.png_compression")
    if "reference_sampling" in worker:
        common._equal(worker["reference_sampling"], REFERENCE_SAMPLING, f"{location}.worker_renderer_identity.reference_sampling")

    audit = common._object(receipt["benchmark_audit"], f"{location}.benchmark_audit")
    common._exact_keys(audit, common.AUDIT_KEYS, f"{location}.benchmark_audit")
    common._equal(audit["sample_ranges"], receipt["sample_ranges"], f"{location}.benchmark_audit.sample_ranges")
    common._equal(audit["passed"], True, f"{location}.benchmark_audit.passed")
    common._equal(audit["global_agreement"], screen["benchmark_global_agreement"], f"{location}.benchmark_audit.global_agreement")
    common._equal(audit["worst_tile_agreement"], screen["benchmark_worst_regional_agreement"], f"{location}.benchmark_audit.worst_tile_agreement")
    common._equal(audit["worst_microtile_agreement"], screen["benchmark_worst_microtile_agreement"], f"{location}.benchmark_audit.worst_microtile_agreement")
    common._equal(audit["product_decision_used_reference"], False, f"{location}.benchmark_audit.product_decision_used_reference")
    outputs = common._array(receipt["outputs"], f"{location}.outputs")
    if len(outputs) != 1:
        raise common._error(f"{location}.outputs", "must contain exactly one selected draft")
    output = common._object(outputs[0], f"{location}.outputs[0]")
    common._exact_keys(output, {"schema_version", "kind", "manifest_path"}, f"{location}.outputs[0]")
    expected_manifest = str(PurePosixPath(arm["local_artifacts"]["unit_relative_path"]) / "draft-manifest.json")
    common._equal(output["manifest_path"], expected_manifest, f"{location}.outputs[0].manifest_path")

    controller = common._object(receipt["controller_receipt"], f"{location}.controller_receipt")
    common._exact_keys(controller, common.CONTROLLER_KEYS, f"{location}.controller_receipt")
    for key, expected in {
        "units": 1,
        "accepted_units": 1,
        "repaired_units": 0,
        "accepted_fraction": 1.0,
        "repaired_fraction": 0.0,
        "quality_gate": True,
        "repair_cost_s": 0.0,
        "baseline_total_time_s": screen["baseline_256_s"],
        "total_product_time_s": screen["product_s"],
        "speedup_vs_baseline": screen["measured_256_speedup_x"],
    }.items():
        common._equal(controller[key], expected, f"{location}.controller_receipt.{key}")
    component_sum = sum(
        common._number(controller[key], f"{location}.controller_receipt.{key}", minimum=0.0)
        for key in ("draft_cost_s", "verify_cost_s", "overhead_cost_s", "repair_cost_s")
    )
    if abs(component_sum - screen["product_s"]) > 1.1e-6:
        raise common._error(f"{location}.controller_receipt", "component costs disagree with product time")


def _validate_manifest(
    manifest: dict[str, Any], arm: dict[str, Any], receipt: dict[str, Any], phase: str, index: int
) -> None:
    location = f"local.arms[{index}].{phase}_manifest"
    common._exact_keys(manifest, MANIFEST_KEYS, location)
    common._equal(manifest["phase"], phase, f"{location}.phase")
    common._equal(manifest["binding_sha256"], receipt["benchmark_audit"]["binding_sha256"], f"{location}.binding_sha256")
    common._equal(manifest["artifact_verified"], False, f"{location}.artifact_verified")
    common._equal(manifest["billing_eligible"], False, f"{location}.billing_eligible")
    pins = common._object(manifest["pins"], f"{location}.pins")
    common._exact_keys(pins, MANIFEST_PIN_KEYS, f"{location}.pins")
    for key in MANIFEST_PIN_KEYS - {"child_script_sha256"}:
        common._equal(pins[key], receipt["pins"][key], f"{location}.pins.{key}")
    common._sha256(pins["child_script_sha256"], f"{location}.pins.child_script_sha256")
    render = common._object(manifest["render"], f"{location}.render")
    common._exact_keys(render, RENDER_KEYS, f"{location}.render")
    common._equal(render["device"], "GPU/METAL", f"{location}.render.device")
    common._equal(render["engine"], "CYCLES", f"{location}.render.engine")
    common._equal(render["frame"], 1, f"{location}.render.frame")
    common._equal([render["width"], render["height"]], [1920, 1080], f"{location}.render.resolution")
    common._equal(render["png_compression"], 0, f"{location}.render.png_compression")
    common._equal(render["worker_renderer_identity"], receipt["worker_renderer_identity"], f"{location}.render.worker_renderer_identity")
    expected_samples, expected_offset = ((32, 0) if phase == "draft" else (256, 64))
    common._equal(render["samples"], expected_samples, f"{location}.render.samples")
    common._equal(render["sample_offset"], expected_offset, f"{location}.render.sample_offset")
    policy = common._object(render["integrator_policy"], f"{location}.render.integrator_policy")
    policy_keys = {"actual_integrator", "candidate_profile", "mode", "repair_and_baseline_restore_scene_native"}
    if arm["generation"] == "generation_2_sampling_and_light_tree":
        policy_keys |= {"actual_sampling", "samples_are_cap_when_adaptive"}
    common._exact_keys(policy, policy_keys, f"{location}.render.integrator_policy")
    common._equal(policy["candidate_profile"], arm["candidate_profile"], f"{location}.render.integrator_policy.candidate_profile")
    common._equal(policy["repair_and_baseline_restore_scene_native"], True, f"{location}.render.integrator_policy.repair_and_baseline_restore_scene_native")
    candidate_integrator = (
        NATIVE_INTEGRATOR
        if arm["key"] == "native"
        else {
            "diffuse_bounces": arm["candidate_profile"]["diffuse_bounces"],
            "glossy_bounces": arm["candidate_profile"]["glossy_bounces"],
            "max_bounces": arm["candidate_profile"]["max_bounces"],
            "transmission_bounces": arm["candidate_profile"]["transmission_bounces"],
        }
    )
    common._equal(policy["actual_integrator"], candidate_integrator if phase == "draft" else NATIVE_INTEGRATOR, f"{location}.render.integrator_policy.actual_integrator")
    common._equal(policy["mode"], "scene_native" if phase == "baseline" or arm["key"] == "native" else "candidate_capped", f"{location}.render.integrator_policy.mode")
    if "actual_sampling" in policy:
        expected_sampling = ACTUAL_SAMPLING[arm["key"]] if phase == "draft" else REFERENCE_SAMPLING
        common._equal(policy["actual_sampling"], expected_sampling, f"{location}.render.integrator_policy.actual_sampling")
        common._equal(policy["samples_are_cap_when_adaptive"], bool(expected_sampling["use_adaptive_sampling"]), f"{location}.render.integrator_policy.samples_are_cap_when_adaptive")


def _verify_arm_artifacts(
    arm: dict[str, Any], receipt: dict[str, Any] | None, index: int, *, require_local: bool
) -> bytes | None:
    location = f"local.arms[{index}].artifacts"
    root = Path(common._string(arm["local_artifacts"]["root"], f"{location}.root"))
    if not root.exists():
        if require_local:
            raise common._error(f"{location}.root", "local artifacts are required but absent")
        return None
    if root.is_symlink() or not root.is_dir():
        raise common._error(f"{location}.root", "must be a non-symlink directory")
    if receipt is None:
        raise common._error(location, "artifact root exists but its pinned raw receipt is absent")
    unit = PurePosixPath(arm["local_artifacts"]["unit_relative_path"])
    files = arm["local_artifacts"]["files"]
    raw: dict[str, bytes] = {}
    paths: dict[str, Path] = {}
    for name in sorted(FILE_NAMES):
        path = common._contained_file(root, str(unit / name), f"{location}.{name}.path")
        data = common._stable_bytes(path, f"{location}.{name}", maximum=common.MAX_ARTIFACT_BYTES)
        common._equal(len(data), files[name]["bytes"], f"{location}.{name}.bytes")
        common._equal(_digest(data), files[name]["sha256"], f"{location}.{name}.sha256")
        if name.endswith(".png") and not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise common._error(f"{location}.{name}", "is not a PNG")
        raw[name] = data
        paths[name] = path
    draft = common._strict_json_bytes(raw["draft-manifest.json"], f"{location}.draft-manifest.json")
    baseline = common._strict_json_bytes(raw["baseline-manifest.json"], f"{location}.baseline-manifest.json")
    verification = common._strict_json_bytes(raw["verification-manifest.json"], f"{location}.verification-manifest.json")
    audit = common._strict_json_bytes(raw["benchmark-audit.json"], f"{location}.benchmark-audit.json")
    common._equal(audit, receipt["benchmark_audit"], f"{location}.benchmark-audit.json")
    _validate_manifest(draft, arm, receipt, "draft", index)
    _validate_manifest(baseline, arm, receipt, "baseline", index)
    for phase, manifest in (("draft", draft), ("baseline", baseline)):
        artifact = common._object(manifest["artifact"], f"{location}.{phase}-manifest.json.artifact")
        common._exact_keys(artifact, {"media_type", "path", "sha256"}, f"{location}.{phase}-manifest.json.artifact")
        name = f"{phase}.png"
        common._equal(artifact["path"], str(unit / name), f"{location}.{phase}-manifest.json.artifact.path")
        common._equal(artifact["sha256"], files[name]["sha256"], f"{location}.{phase}-manifest.json.artifact.sha256")
    common._equal(draft["artifact"]["sha256"], receipt["benchmark_audit"]["candidate"]["artifact_sha256"], f"{location}.draft-artifact-receipt-pin")
    common._equal(baseline["artifact"]["sha256"], receipt["benchmark_audit"]["baseline"]["artifact_sha256"], f"{location}.baseline-artifact-receipt-pin")

    common._exact_keys(verification, common.VERIFICATION_KEYS, f"{location}.verification-manifest.json")
    common._equal(verification["accepted"], True, f"{location}.verification-manifest.json.accepted")
    common._equal(verification["binding_sha256"], receipt["benchmark_audit"]["binding_sha256"], f"{location}.verification-manifest.json.binding_sha256")
    common._equal(verification["selected_manifest_path"], str(unit / "draft-manifest.json"), f"{location}.verification-manifest.json.selected_manifest_path")
    common._equal(verification["draft_sample_offset"], 0, f"{location}.verification-manifest.json.draft_sample_offset")
    common._equal(verification["verify_sample_offset"], 32, f"{location}.verification-manifest.json.verify_sample_offset")
    common._equal(verification["sample_ranges_disjoint"], True, f"{location}.verification-manifest.json.sample_ranges_disjoint")
    product = arm["product_gate"]
    for key, source in (
        ("global_agreement", "global_agreement"),
        ("worst_tile_agreement", "worst_regional_agreement"),
        ("worst_microtile_agreement", "worst_microtile_agreement"),
    ):
        common._equal(verification[key], product[source], f"{location}.verification-manifest.json.{key}")
    verify_artifact = common._object(verification["verify_artifact"], f"{location}.verification-manifest.json.verify_artifact")
    common._exact_keys(verify_artifact, {"path", "sha256"}, f"{location}.verification-manifest.json.verify_artifact")
    common._equal(verify_artifact["path"], str(unit / "verify.png"), f"{location}.verification-manifest.json.verify_artifact.path")
    common._equal(verify_artifact["sha256"], files["verify.png"]["sha256"], f"{location}.verification-manifest.json.verify_artifact.sha256")
    return raw["draft.png"]


def _verify_historical(
    history: dict[str, Any], *, require_local: bool
) -> tuple[dict[str, Any], bytes | None]:
    receipt_path = _resolve_path(history["receipt_path"], "proof.protocol.historical_4096_reference.receipt_path")
    receipt, receipt_bytes = common._read_json(receipt_path, "historical.receipt")
    common._equal(len(receipt_bytes), history["receipt_bytes"], "historical.receipt.bytes")
    common._equal(_digest(receipt_bytes), history["receipt_sha256"], "historical.receipt.sha256")
    common._exact_keys(receipt, common.RECEIPT_KEYS, "historical.receipt")
    common._equal(receipt["scene_sha256"], SCENE_SHA256, "historical.receipt.scene_sha256")
    common._equal(receipt["baseline_s"], HISTORICAL_BASELINE_S, "historical.receipt.baseline_s")
    common._equal(receipt["reference_samples"], 4096, "historical.receipt.reference_samples")
    common._equal(receipt["benchmark_audit"]["sample_ranges"]["baseline"], [64, 4160], "historical.receipt.benchmark_audit.sample_ranges.baseline")
    root = Path(history["artifact_root"])
    if not root.exists():
        if require_local:
            raise common._error("historical.artifact_root", "local artifacts are required but absent")
        return receipt, None
    if root.is_symlink() or not root.is_dir():
        raise common._error("historical.artifact_root", "must be a non-symlink directory")
    unit = PurePosixPath(history["unit_relative_path"])
    manifest_path = common._contained_file(root, str(unit / "baseline-manifest.json"), "historical.baseline_manifest.path")
    png_path = common._contained_file(root, str(unit / "baseline.png"), "historical.baseline_png.path")
    manifest, manifest_bytes = common._read_json(manifest_path, "historical.baseline_manifest")
    png = common._stable_bytes(png_path, "historical.baseline_png", maximum=common.MAX_ARTIFACT_BYTES)
    for name, data in (("baseline_manifest", manifest_bytes), ("baseline_png", png)):
        common._equal(len(data), history[name]["bytes"], f"historical.{name}.bytes")
        common._equal(_digest(data), history[name]["sha256"], f"historical.{name}.sha256")
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise common._error("historical.baseline_png", "is not a PNG")
    common._exact_keys(manifest, MANIFEST_KEYS, "historical.baseline_manifest")
    common._equal(manifest["phase"], "baseline", "historical.baseline_manifest.phase")
    common._equal(manifest["render"]["samples"], 4096, "historical.baseline_manifest.render.samples")
    common._equal(manifest["render"]["sample_offset"], 64, "historical.baseline_manifest.render.sample_offset")
    common._equal(manifest["artifact"]["path"], str(unit / "baseline.png"), "historical.baseline_manifest.artifact.path")
    common._equal(manifest["artifact"]["sha256"], history["baseline_png"]["sha256"], "historical.baseline_manifest.artifact.sha256")
    common._equal(receipt["benchmark_audit"]["baseline"]["artifact_sha256"], history["baseline_png"]["sha256"], "historical.receipt.baseline.artifact_sha256")
    return receipt, png


def _agreement(a_bytes: bytes, b_bytes: bytes) -> tuple[float, float, float]:
    try:
        from PIL import Image, ImageChops, ImageStat, UnidentifiedImageError
    except ImportError as exc:  # pragma: no cover - local-only dependency boundary
        raise common._error("local.retained_4096_audit", "Pillow is required to recompute local image audits") from exc
    try:
        with Image.open(BytesIO(a_bytes)) as a_source, Image.open(BytesIO(b_bytes)) as b_source:
            if a_source.format != "PNG" or b_source.format != "PNG" or a_source.size != (1920, 1080) or b_source.size != (1920, 1080):
                raise common._error("local.retained_4096_audit", "PNG shape or format mismatch")
            a = a_source.convert("RGB")
            b = b_source.convert("RGB")
            a.load()
            b.load()
    except (OSError, UnidentifiedImageError) as exc:
        raise common._error("local.retained_4096_audit", f"cannot decode PNG: {exc}") from exc
    difference = ImageChops.difference(a, b)

    def score(image: Any) -> float:
        means = ImageStat.Stat(image).mean
        return min(1.0, max(0.0, 1.0 - sum(means) / (len(means) * 255.0)))

    def minimum(columns: int, rows: int) -> float:
        values = []
        for row in range(rows):
            top, bottom = row * 1080 // rows, (row + 1) * 1080 // rows
            for column in range(columns):
                left, right = column * 1920 // columns, (column + 1) * 1920 // columns
                values.append(score(difference.crop((left, top, right, bottom))))
        return min(values)

    return round(score(difference), 9), round(minimum(16, 9), 9), round(minimum(60, 33), 9)


def verify(
    proof_path: Path = DEFAULT_PROOF, *, require_local_artifacts: bool = False
) -> dict[str, Any]:
    proof, proof_bytes = common._read_json(proof_path, "proof")
    _validate_structure(proof)
    semantic_sha = _semantic_digest(proof)
    common._equal(semantic_sha, EXPECTED_SEMANTIC_SHA256, "proof.semantic_sha256")
    _validate_semantics(proof)

    history = proof["protocol"]["historical_4096_reference"]
    _, reference_png = _verify_historical(history, require_local=require_local_artifacts)
    receipt_count = 0
    artifact_count = 0
    recomputed_count = 0
    for index, arm in enumerate(proof["arms"]):
        receipt_path = _resolve_path(arm["receipt"]["path"], f"proof.arms[{index}].receipt.path")
        receipt: dict[str, Any] | None = None
        if receipt_path.exists():
            receipt, receipt_bytes = common._read_json(receipt_path, f"local.arms[{index}].receipt")
            _validate_receipt(receipt, receipt_bytes, arm, index)
            receipt_count += 1
        elif require_local_artifacts:
            raise common._error(f"local.arms[{index}].receipt", "local raw receipt is required but absent")
        draft_png = _verify_arm_artifacts(
            arm, receipt, index, require_local=require_local_artifacts
        )
        if draft_png is not None:
            artifact_count += 1
        if draft_png is not None and reference_png is not None:
            actual = _agreement(draft_png, reference_png)
            retained = arm["retained_4096_audit"]
            expected = (
                retained["global_agreement"],
                retained["worst_regional_agreement"],
                retained["worst_microtile_agreement"],
            )
            common._equal(actual, expected, f"local.arms[{index}].retained_4096_audit")
            recomputed_count += 1
    local_complete = (
        receipt_count == len(ARM_ORDER)
        and artifact_count == len(ARM_ORDER)
        and recomputed_count == len(ARM_ORDER)
        and reference_png is not None
    )
    if require_local_artifacts and not local_complete:
        raise common._error("local", "required local corroboration is incomplete")
    return {
        "schema_version": 1,
        "kind": RESULT_KIND,
        "valid": True,
        "status": "pruned_negative_screen",
        "proof_sha256": _digest(proof_bytes),
        "semantic_sha256": semantic_sha,
        "arms_screened": 8,
        "best_arm": "cap8_v1",
        "best_cross_session_projection_x": 40.292637,
        "native_cross_session_projection_x": 40.07152,
        "cross_session_projection_is_measured": False,
        "meets_50x": False,
        "local_receipts_verified": receipt_count,
        "local_artifact_arms_verified": artifact_count,
        "retained_4096_audits_recomputed": recomputed_count,
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
