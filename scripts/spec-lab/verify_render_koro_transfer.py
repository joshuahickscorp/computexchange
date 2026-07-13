#!/usr/bin/env python3
"""Fail-closed verifier for the held-out Koro portrait transfer proof.

The committed receipt is portable, local-unattested metadata.  If the pinned
source and render caches remain present, this verifier hashes the complete
nine-file source bundle, every decisive manifest and PNG, and recomputes both
the reference-free product gate and the fresh 4096-spp audit.  Local cache
files are optional unless ``--require-local-artifacts`` is supplied.
"""

from __future__ import annotations

import argparse
from io import BytesIO
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
from typing import Any

import verify_render_transfer_matrix as common


HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
DEFAULT_RECEIPT = (
    REPO
    / "proof/performance/apple-metal-render-transfer-koro-portrait-2026-07-12.json"
)
DEFAULT_PROVENANCE = (
    REPO
    / "proof/performance/apple-metal-render-transfer-koro-portrait-provenance-2026-07-12.json"
)
RESULT_KIND = "cx_render_koro_transfer_verification"
PROVENANCE_KIND = "apple_metal_render_koro_transfer_provenance"
SOURCE_URL = (
    "https://download.blender.org/release/BlenderBenchmark2.0/scenes/koro.tar.bz2"
)
ARCHIVE_BYTES = 61_246_985
ARCHIVE_SHA256 = "c6c81d9eebb604322b99c7d58f2b9f8827b6c11fe10b0bc5c0edb4d01a878d63"
SCENE_BYTES = 47_122_672
SCENE_SHA256 = "12fdca959492d4cb59a06ca837e2e7baa87d1ef6ac67648a88758089a7b14c07"
BUNDLE_BYTES = 91_086_827
BUNDLE_SHA256 = "f846b4261c12d5b1f5310df631cf94756ed72b96298003a4bf0a6c64c2071874"
RECEIPT_RAW_SHA256 = "b1ca40bc63288ad8736b76592c24a9baffe168f752c8309ac6925cc2c22f19c8"
RECEIPT_CANONICAL_SHA256 = "7b3d56cc647cc6e03b6c80067ef5766094838ed550bada3993621ea0d8818ab4"
EXPECTED_BINDING_SHA256 = "e2aef8545b1e81c8f20b72fa6ca7b7c044367134bee1f4bafd238af82a5c483b"
RESOLUTION = [1080, 1920]
SAMPLE_RANGES = {"draft": [0, 4], "verify": [4, 8], "baseline": [8, 4104]}

BUNDLE_ENTRIES = [
    {
        "path": "background.png",
        "bytes": 263020,
        "sha256": "9364ec67612ed70ff9b5192675f8813f3d9168b15d7320e6a80ea7f87a9e2902",
    },
    {
        "path": "main.blend",
        "bytes": SCENE_BYTES,
        "sha256": SCENE_SHA256,
    },
    {
        "path": "textures/koro_bump.png",
        "bytes": 12605396,
        "sha256": "9dad173a4ea56b9758e23018fb06339b1ae8ff90faf03eeb8e9a9a9f573592ae",
    },
    {
        "path": "textures/koro_color.png",
        "bytes": 16806489,
        "sha256": "1aa67c6cd1af8c1581a4fac817b3ee4a016c416c9fc01ebc2fcf8ca5a28d6ae8",
    },
    {
        "path": "textures/koro_color_4k.png",
        "bytes": 10225002,
        "sha256": "265bf5db50d837e618e001e0ab6bd66b95b6edadf9cddb5b9754f655a834cfa4",
    },
    {
        "path": "textures/koro_eye.png",
        "bytes": 704582,
        "sha256": "cf490c5ee3d87fa96f6e16ce21e9d0937d8718dd319ed3bc7fc9284d84471f3b",
    },
    {
        "path": "textures/koro_hair_alpha.png",
        "bytes": 159335,
        "sha256": "2141261144c78b7b04b5ec14dcbc715b1f2685311aa336fdd8f525c715ac2654",
    },
    {
        "path": "textures/koro_hair_jeta_alpha.png",
        "bytes": 3151924,
        "sha256": "4e5545fc66944f966fa7227e672d93e36ec887d4b860fbca04c3a5e8f6eadb7a",
    },
    {
        "path": "textures/koro_spec.png",
        "bytes": 48407,
        "sha256": "32c8cd799aacb9a8e1b8c9847cffc07443f525ce69aa7f86a89fe1440ca8f4c3",
    },
]

PARTICLE_SYSTEMS = [
    {"object": "koro_mesh_body", "name": "fur_base", "settings": "sets_fur_main.002", "type": "HAIR", "settings_count": 0, "stored_particles": 272, "child_type": "INTERPOLATED", "viewport_children_per_parent": 200, "configured_viewport_children": 54400, "render_children_per_parent": 500, "configured_render_children": 136000},
    {"object": "koro_mesh_body", "name": "fur_fluffychest", "settings": "sets_fur_main.003", "type": "HAIR", "settings_count": 0, "stored_particles": 123, "child_type": "INTERPOLATED", "viewport_children_per_parent": 180, "configured_viewport_children": 22140, "render_children_per_parent": 180, "configured_render_children": 22140},
    {"object": "koro_mesh_body", "name": "fur_face_c", "settings": "fur_face", "type": "HAIR", "settings_count": 0, "stored_particles": 105, "child_type": "INTERPOLATED", "viewport_children_per_parent": 500, "configured_viewport_children": 52500, "render_children_per_parent": 2000, "configured_render_children": 210000},
    {"object": "koro_mesh_body", "name": "fur_long_single", "settings": "fur_long_single", "type": "HAIR", "settings_count": 500, "stored_particles": 498, "child_type": "INTERPOLATED", "viewport_children_per_parent": 5, "configured_viewport_children": 2490, "render_children_per_parent": 5, "configured_render_children": 2490},
    {"object": "koro_mesh_body", "name": "fur_messy", "settings": "sets_fur_main.000", "type": "HAIR", "settings_count": 0, "stored_particles": 162, "child_type": "INTERPOLATED", "viewport_children_per_parent": 200, "configured_viewport_children": 32400, "render_children_per_parent": 200, "configured_render_children": 32400},
    {"object": "koro_mesh_body", "name": "fur_exploded", "settings": "ParticleSettings", "type": "HAIR", "settings_count": 100, "stored_particles": 371, "child_type": "INTERPOLATED", "viewport_children_per_parent": 150, "configured_viewport_children": 55650, "render_children_per_parent": 1000, "configured_render_children": 371000},
    {"object": "koro_mesh_body", "name": "fur_face_burnt", "settings": "fur_face_burnt", "type": "HAIR", "settings_count": 0, "stored_particles": 65, "child_type": "INTERPOLATED", "viewport_children_per_parent": 100, "configured_viewport_children": 6500, "render_children_per_parent": 500, "configured_render_children": 32500},
]

PINS = {
    "backend_sha256": "1e1b156663a0d3242874294a87194de59cc283ffb1f983106e15ed6a52e94e22",
    "benchmark_harness_sha256": "22b6e0e43c2c694466b625f10398b114bd0a2cd6638884ab0d1fb6429e086790",
    "blender_sha256": "b5737da97b0e164cc18e227be115c4ea2791d11e9577f7e302c73f4871f9249c",
    "controller_adapter_sha256": "fbd72047b0497a6ce886c22bb86ba4a4b374a5ebd05b86cc1eb4c2d91afd534e",
    "controller_core_sha256": "c729c8b9e5d231f9c6566d1bc8c492af2ecad4fc2aade7cf2a188f35428d9a11",
    "render_preview_driver_sha256": "5d05da0214b9dab5446910a303691b2a5b342a6658a22e1d4aa0faf07abc1e6b",
}
NATIVE_PROFILE = {
    "adaptive_min_samples": None,
    "adaptive_threshold": None,
    "diffuse_bounces": None,
    "glossy_bounces": None,
    "max_bounces": None,
    "name": "native",
    "transmission_bounces": None,
    "use_adaptive_sampling": False,
    "use_light_tree": None,
}
NATIVE_INTEGRATOR = {
    "diffuse_bounces": 1,
    "glossy_bounces": 1,
    "max_bounces": 2,
    "transmission_bounces": 12,
}
REFERENCE_SAMPLING = {
    "adaptive_min_samples": 0,
    "adaptive_threshold": 0.009999999776482582,
    "use_adaptive_sampling": False,
    "use_light_tree": False,
}

PROVENANCE_KEYS = {
    "schema_version", "kind", "created_at_utc", "status", "claim", "receipt",
    "source", "scene_audit", "selection_protocol", "execution", "quality",
    "local_artifacts", "limitations",
}
MANIFEST_KEYS = {
    "artifact", "artifact_verified", "billing_eligible", "binding_sha256",
    "evidence", "execution_identity_revalidation", "kind", "phase", "pins",
    "preview_only", "production_ready", "render", "scene", "schema_version",
    "unit_id",
}
RENDER_KEYS = {
    "device", "engine", "frame", "height", "integrator_policy", "pixel_filter",
    "png_compression", "sample_offset", "samples", "seed", "width",
    "worker_renderer_identity",
}
WORKER_KEYS = {
    "blender_build_hash", "blender_version", "candidate_profile",
    "dependency_count", "dependency_paths_sha256", "device",
    "enabled_device_names", "native_integrator", "png_compression",
    "reference_sampling",
}
LOCAL_FILE_NAMES = {
    "draft-manifest.json", "verification-manifest.json", "baseline-manifest.json",
    "benchmark-audit.json", "draft.png", "verify.png", "baseline.png",
}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _absolute_path(value: Any, location: str) -> Path:
    raw = common._string(value, location)
    if "\x00" in raw:
        raise common._error(location, "contains a NUL byte")
    path = Path(raw)
    if not path.is_absolute():
        raise common._error(location, "must be an absolute local cache path")
    return path


def _bundle_digest(entries: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        relative = entry["path"].encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(entry["bytes"].to_bytes(8, "big"))
        digest.update(bytes.fromhex(entry["sha256"]))
    return digest.hexdigest()


def _recompute_binding_sha256() -> str:
    """Reproduce the pinned backend's v2 operator-policy binding exactly."""
    value = {
        "binding_policy": "render-preview-operator-policy-v2",
        "modality": "render",
        "operator_policy": {
            "candidate_profile": NATIVE_PROFILE,
            "candidate_profile_scope": "benchmark_screen_v1",
            "profile_authorization": "native_only",
            "png_compression": 0,
        },
        "payload": {
            "scene_path": "main.blend",
            "scene_sha256": SCENE_SHA256,
            "width": 1080,
            "height": 1920,
            "frame": 9,
            "draft_samples": 4,
            "verify_samples": 4,
            "repair_samples": 4096,
        },
        "unit_id": "local-metal-benchmark",
    }
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return _digest(encoded)


def _validate_worker(value: Any, location: str) -> dict[str, Any]:
    worker = common._object(value, location)
    common._exact_keys(worker, WORKER_KEYS, location)
    expected = {
        "blender_build_hash": "396f546c9d82",
        "blender_version": "4.2.1 LTS",
        "candidate_profile": NATIVE_PROFILE,
        "dependency_count": 2,
        "dependency_paths_sha256": "c9aed6ab466d9756677f393d85c7e45c38c4b07afa1dff845306e64cbe068803",
        "device": "GPU/METAL",
        "enabled_device_names": ["Apple M3 Ultra (GPU - 60 cores)"],
        "native_integrator": NATIVE_INTEGRATOR,
        "png_compression": 0,
        "reference_sampling": REFERENCE_SAMPLING,
    }
    common._equal(worker, expected, location)
    return worker


def _validate_controller(receipt: dict[str, Any], baseline: float, product: float) -> None:
    location = "receipt.controller_receipt"
    controller = common._object(receipt["controller_receipt"], location)
    common._exact_keys(controller, common.CONTROLLER_KEYS, location)
    constants = {
        "schema_version": 1, "units": 1, "accepted_units": 1,
        "repaired_units": 0, "accepted_fraction": 1.0, "repaired_fraction": 0.0,
        "quality_gate": True, "repair_cost_s": 0.0, "artifact_verified": False,
        "baseline_source": "measured", "evidence": "measured", "modality": "render",
        "quality_tier": "preview", "branch_id": "local-cycles-spec-benchmark-v1",
        "exact": False, "global_ssim": None, "p5_ssim": None,
        "worst_tile_ssim": None,
    }
    for key, expected in constants.items():
        common._equal(controller[key], expected, f"{location}.{key}")
    common._equal(controller["baseline_total_time_s"], baseline, f"{location}.baseline_total_time_s")
    common._equal(controller["total_product_time_s"], product, f"{location}.total_product_time_s")
    common._equal(controller["speedup_vs_baseline"], receipt["speedup_x"], f"{location}.speedup_vs_baseline")
    component_sum = sum(
        common._number(controller[key], f"{location}.{key}", minimum=0.0)
        for key in ("draft_cost_s", "verify_cost_s", "overhead_cost_s", "repair_cost_s")
    )
    common._equal(round(component_sum, 6), product, location)
    details = common._object(controller["details"], f"{location}.details")
    common._exact_keys(details, {"engine_benchmark", "engine_speedup_x", "quality_gate_spec", "source"}, f"{location}.details")
    common._equal(details["quality_gate_spec"], "g>=0.9,wt>=0.85", f"{location}.details.quality_gate_spec")
    common._equal(details["source"], "cx_speculative_core.SpecReceipt", f"{location}.details.source")
    common._equal(controller["claim_scope"], "MEASURED single delivered unit ratio only; per-tile ratios are NOT multiplied. baseline_total_time_s is a measured reference-quality render for this unit.", f"{location}.claim_scope")
    engine = common._object(details["engine_benchmark"], f"{location}.details.engine_benchmark")
    common._exact_keys(engine, {"baseline_audit_s", "baseline_comparable", "benchmark_mode", "candidate_exact", "counterfactual_baseline_s", "disposition"}, f"{location}.details.engine_benchmark")
    for key, expected in {"baseline_comparable": True, "benchmark_mode": True, "candidate_exact": True, "disposition": "candidate"}.items():
        common._equal(engine[key], expected, f"{location}.details.engine_benchmark.{key}")
    common._number(engine["baseline_audit_s"], f"{location}.details.engine_benchmark.baseline_audit_s", minimum=0.0)
    counterfactual = common._number(engine["counterfactual_baseline_s"], f"{location}.details.engine_benchmark.counterfactual_baseline_s", minimum=0.0)
    common._equal(round(counterfactual, 6), baseline, f"{location}.details.engine_benchmark.counterfactual_baseline_s")
    engine_speed = common._number(details["engine_speedup_x"], f"{location}.details.engine_speedup_x", minimum=0.0)
    if abs(engine_speed - receipt["speedup_x"]) > 0.00001:
        raise common._error(f"{location}.details.engine_speedup_x", "disagrees with the delivered-unit speedup")


def _validate_audit(receipt: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    location = "receipt.benchmark_audit"
    audit = common._object(receipt["benchmark_audit"], location)
    common._exact_keys(audit, common.AUDIT_KEYS, location)
    constants = {
        "schema_version": 2,
        "kind": "cx_cycles_preview_benchmark_audit",
        "measurement_only": True,
        "product_decision_used_reference": False,
        "metric": common.QUALITY_METRIC,
        "global_min": common.GLOBAL_MIN,
        "worst_tile_min": common.REGIONAL_MIN,
        "worst_microtile_min": common.MICROTILE_MIN,
        "sample_ranges": SAMPLE_RANGES,
        "sample_ranges_disjoint": True,
        "tile_contract": "resolution_relative_regions_not_fixed_pixel_defects",
        "microtile_contract": "fixed_scale_catastrophic_defect_sentinel",
        "tile_count": 144,
        "tile_grid": {"columns": 9, "max_long_edge_tiles": 16, "minimum_nominal_edge_pixels": 32, "rows": 16},
        "microtile_count": 1980,
        "microtile_grid": {"columns": 33, "nominal_edge_pixels": 32, "rows": 60},
    }
    for key, expected in constants.items():
        common._equal(audit[key], expected, f"{location}.{key}")
    ranges = common._ranges(audit["sample_ranges"], f"{location}.sample_ranges")
    common._equal(ranges, {"draft": (0, 4), "verify": (4, 8), "baseline": (8, 4104)}, f"{location}.sample_ranges")
    candidate = common._object(audit["candidate"], f"{location}.candidate")
    baseline = common._object(audit["baseline"], f"{location}.baseline")
    descriptor_keys = {"artifact_sha256", "manifest_path", "phase", "sample_offset"}
    common._exact_keys(candidate, descriptor_keys, f"{location}.candidate")
    common._exact_keys(baseline, descriptor_keys, f"{location}.baseline")
    common._equal(candidate["phase"], "draft", f"{location}.candidate.phase")
    common._equal(candidate["sample_offset"], 0, f"{location}.candidate.sample_offset")
    common._equal(candidate["manifest_path"], output["manifest_path"], f"{location}.candidate.manifest_path")
    common._sha256(candidate["artifact_sha256"], f"{location}.candidate.artifact_sha256")
    common._equal(baseline["phase"], "baseline", f"{location}.baseline.phase")
    common._equal(baseline["sample_offset"], 8, f"{location}.baseline.sample_offset")
    common._relative_path(baseline["manifest_path"], f"{location}.baseline.manifest_path")
    common._sha256(baseline["artifact_sha256"], f"{location}.baseline.artifact_sha256")
    scores = {
        "global": common._number(audit["global_agreement"], f"{location}.global_agreement", minimum=0.0, maximum=1.0),
        "regional": common._number(audit["worst_tile_agreement"], f"{location}.worst_tile_agreement", minimum=0.0, maximum=1.0),
        "micro": common._number(audit["worst_microtile_agreement"], f"{location}.worst_microtile_agreement", minimum=0.0, maximum=1.0),
    }
    passed = scores["global"] >= common.GLOBAL_MIN and scores["regional"] >= common.REGIONAL_MIN and scores["micro"] >= common.MICROTILE_MIN
    common._equal(audit["passed"], passed, f"{location}.passed")
    if not passed:
        raise common._error(location, "reference audit did not pass")
    common._equal(receipt["global_agreement"], scores["global"], "receipt.global_agreement")
    common._equal(receipt["worst_tile_agreement"], scores["regional"], "receipt.worst_tile_agreement")
    recomputed_binding = _recompute_binding_sha256()
    common._equal(recomputed_binding, EXPECTED_BINDING_SHA256, f"{location}.binding_recomputation")
    common._equal(audit["binding_sha256"], recomputed_binding, f"{location}.binding_sha256")
    return {**scores, "binding": audit["binding_sha256"], "candidate_sha256": candidate["artifact_sha256"], "baseline_sha256": baseline["artifact_sha256"]}


def _validate_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    location = "receipt"
    common._exact_keys(receipt, common.RECEIPT_KEYS, location)
    constants = {
        "schema_version": 1, "kind": common.RECEIPT_KIND, "evidence": "measured",
        "receipt_trust": "local_unattested", "preview_only": True,
        "production_ready": False, "cache_used": False, "cold_start_included": False,
        "trial_count": 1, "variance_estimate": None, "warmup_candidate_runs": 1,
        "sample_ranges_disjoint": True, "quality_gate": True,
        "quality_metric": common.QUALITY_METRIC, "reference_used_for_product_decision": False,
        "device": "GPU/METAL", "frame": 9, "resolution": RESOLUTION,
        "draft_samples": 4, "verify_samples": 4, "reference_samples": 4096,
        "sample_ranges": SAMPLE_RANGES,
        "timing_scope": "resident_steady_state_after_uncharged_warmup",
        "timing_statistics": "fixed-order single trial; no variance estimate",
        "execution_identity_revalidation": "initial_sha256_plus_pre_and_post_stat_identity_and_bundle_file_set",
    }
    for key, expected in constants.items():
        common._equal(receipt[key], expected, f"{location}.{key}")
    common._equal(receipt["execution_order"], ["uncharged_full_candidate_warmup", "measured_baseline", "measured_candidate", "measurement_only_baseline_audit"], f"{location}.execution_order")
    common._equal(receipt["order_bias_control"], "full candidate path warmed before baseline", f"{location}.order_bias_control")
    common._equal(receipt["claim_scope"], "single-frame resident steady-state preview ratio; fresh high-SPP Cycles baseline and reference-free spec path on the same pinned session", f"{location}.claim_scope")
    common._equal(receipt["quality_contract"], "8-bit display-RGB mean-absolute agreement over a resolution-relative regional grid plus a fixed-scale catastrophic microtile sentinel; not SSIM or perceptual equivalence", f"{location}.quality_contract")
    common._equal(receipt["scene_sha256"], SCENE_SHA256, f"{location}.scene_sha256")
    _absolute_path(receipt["scene"], f"{location}.scene")
    _absolute_path(receipt["artifact_root"], f"{location}.artifact_root")
    common._number(receipt["benchmark_wall_s"], f"{location}.benchmark_wall_s", minimum=0.0)
    common._number(receipt["warmup_s_uncharged"], f"{location}.warmup_s_uncharged", minimum=0.0)
    ranges = common._ranges(receipt["sample_ranges"], f"{location}.sample_ranges")
    common._equal(ranges, {"draft": (0, 4), "verify": (4, 8), "baseline": (8, 4104)}, f"{location}.sample_ranges")
    baseline = common._number(receipt["baseline_s"], f"{location}.baseline_s", minimum=0.000001)
    product = common._number(receipt["spec_s"], f"{location}.spec_s", minimum=0.000001)
    speedup = common._number(receipt["speedup_x"], f"{location}.speedup_x", minimum=0.0)
    common._equal(baseline, 112.396726, f"{location}.baseline_s")
    common._equal(product, 1.334074, f"{location}.spec_s")
    common._equal(speedup, 84.250743, f"{location}.speedup_x")
    common._equal(speedup, round(baseline / product, 6), f"{location}.speedup_x")
    common._equal(receipt["meets_50x_preview_experiment"], speedup >= 50.0, f"{location}.meets_50x_preview_experiment")
    common._equal(receipt["meets_100x_preview_experiment"], speedup >= 100.0, f"{location}.meets_100x_preview_experiment")
    if speedup < 50.0 or speedup >= 100.0:
        raise common._error(location, "must support only the measured 50x-to-100x preview band")
    outputs = common._array(receipt["outputs"], f"{location}.outputs")
    if len(outputs) != 1:
        raise common._error(f"{location}.outputs", "must contain one selected draft")
    output = common._object(outputs[0], f"{location}.outputs[0]")
    common._exact_keys(output, {"schema_version", "kind", "manifest_path"}, f"{location}.outputs[0]")
    common._equal(output["schema_version"], 2, f"{location}.outputs[0].schema_version")
    common._equal(output["kind"], "cx_cycles_preview_artifact", f"{location}.outputs[0].kind")
    common._relative_path(output["manifest_path"], f"{location}.outputs[0].manifest_path")
    if not output["manifest_path"].endswith("/draft-manifest.json"):
        raise common._error(f"{location}.outputs[0].manifest_path", "must select a draft manifest")
    pins = common._object(receipt["pins"], f"{location}.pins")
    common._exact_keys(pins, set(PINS), f"{location}.pins")
    common._equal(pins, PINS, f"{location}.pins")
    worker = _validate_worker(receipt["worker_renderer_identity"], f"{location}.worker_renderer_identity")
    host = common._object(receipt["host"], f"{location}.host")
    common._exact_keys(host, {"cpu_brand", "machine", "memory_bytes", "platform", "python"}, f"{location}.host")
    common._equal(host["cpu_brand"], "Apple M3 Ultra", f"{location}.host.cpu_brand")
    common._equal(host["machine"], "arm64", f"{location}.host.machine")
    try:
        if int(common._string(host["memory_bytes"], f"{location}.host.memory_bytes"), 10) <= 0:
            raise ValueError
    except ValueError as exc:
        raise common._error(f"{location}.host.memory_bytes", "must be a positive decimal integer string") from exc
    renderer = common._object(receipt["renderer_identity"], f"{location}.renderer_identity")
    common._exact_keys(renderer, {"apple_bundle_seal_valid", "apple_signature_checked", "apple_signature_valid", "authorities", "bundle_identifier", "official_signed_executable", "runtime_bundle", "team_identifier", "trust_scope", "version", "version_output_sha256"}, f"{location}.renderer_identity")
    for key, expected in {"apple_bundle_seal_valid": False, "apple_signature_checked": True, "apple_signature_valid": True, "bundle_identifier": "org.blenderfoundation.blender", "official_signed_executable": True, "team_identifier": "68UA947AUU", "version": "Blender 4.2.1 LTS"}.items():
        common._equal(renderer[key], expected, f"{location}.renderer_identity.{key}")
    common._sha256(renderer["version_output_sha256"], f"{location}.renderer_identity.version_output_sha256")
    runtime = common._object(renderer["runtime_bundle"], f"{location}.renderer_identity.runtime_bundle")
    common._exact_keys(runtime, {"all_regular_files_included", "bytes", "files", "root", "sha256"}, f"{location}.renderer_identity.runtime_bundle")
    common._equal(runtime["all_regular_files_included"], True, f"{location}.renderer_identity.runtime_bundle.all_regular_files_included")
    common._sha256(runtime["sha256"], f"{location}.renderer_identity.runtime_bundle.sha256")
    _validate_controller(receipt, baseline, product)
    audit = _validate_audit(receipt, output)
    return {"baseline": baseline, "product": product, "speedup": speedup, "output": output, "pins": pins, "worker": worker, "audit": audit}


def _validate_source(source: dict[str, Any]) -> None:
    location = "provenance.source"
    common._exact_keys(source, {"publisher", "url", "archive_path", "archive_bytes", "archive_sha256", "scene_archive_path", "scene_bytes", "scene_sha256", "bundle_root", "bundle"}, location)
    expected = {"publisher": "Blender Foundation download host", "url": SOURCE_URL, "archive_bytes": ARCHIVE_BYTES, "archive_sha256": ARCHIVE_SHA256, "scene_archive_path": "koro/main.blend", "scene_bytes": SCENE_BYTES, "scene_sha256": SCENE_SHA256}
    for key, value in expected.items():
        common._equal(source[key], value, f"{location}.{key}")
    _absolute_path(source["archive_path"], f"{location}.archive_path")
    _absolute_path(source["bundle_root"], f"{location}.bundle_root")
    bundle = common._object(source["bundle"], f"{location}.bundle")
    common._exact_keys(bundle, {"files", "bytes", "sha256", "canonical_digest_method", "entries"}, f"{location}.bundle")
    common._equal(bundle["files"], len(BUNDLE_ENTRIES), f"{location}.bundle.files")
    common._equal(bundle["bytes"], BUNDLE_BYTES, f"{location}.bundle.bytes")
    common._equal(bundle["sha256"], BUNDLE_SHA256, f"{location}.bundle.sha256")
    common._equal(bundle["canonical_digest_method"], "For entries sorted by POSIX path: SHA-256 over uint64be(path_utf8_bytes), path UTF-8, uint64be(file_bytes), and the 32 raw SHA-256 bytes", f"{location}.bundle.canonical_digest_method")
    entries = common._array(bundle["entries"], f"{location}.bundle.entries")
    for index, entry in enumerate(entries):
        common._exact_keys(common._object(entry, f"{location}.bundle.entries[{index}]"), {"path", "bytes", "sha256"}, f"{location}.bundle.entries[{index}]")
    common._equal(entries, BUNDLE_ENTRIES, f"{location}.bundle.entries")
    common._equal(_bundle_digest(entries), BUNDLE_SHA256, f"{location}.bundle.sha256")


def _validate_scene_audit(scene: dict[str, Any]) -> int:
    location = "provenance.scene_audit"
    keys = {"auditor", "engine", "native_resolution_width_by_height", "orientation", "camera", "frame_start", "frame_end", "frame_current", "objects", "object_types", "rig", "modifiers", "particle_systems", "stored_hair_particles_total", "configured_viewport_children_total", "configured_render_children_total", "live_unpacked_bundled_images", "modern_curves_data_blocks", "volume_data_blocks", "motion_blur", "frame_1_to_7_draft_change"}
    common._exact_keys(scene, keys, location)
    constants = {
        "engine": "CYCLES", "native_resolution_width_by_height": [720, 1280],
        "orientation": "portrait", "camera": "CAM_closeup.001", "frame_start": 1,
        "frame_end": 9, "frame_current": 7, "objects": 180,
        "object_types": {"MESH": 159, "ARMATURE": 1, "LATTICE": 8, "CAMERA": 2, "LIGHT": 2, "EMPTY": 8},
        "rig": {"name": "koro_blenrig", "bones": 1364},
        "modifiers": {"ARMATURE": 10, "LATTICE": 31, "SUBSURF": 7, "MESH_DEFORM": 1, "WARP": 1, "PARTICLE_SYSTEM": 7, "MASK": 2, "HOOK": 18},
        "particle_systems": PARTICLE_SYSTEMS,
        "live_unpacked_bundled_images": [{"name": "koro_color", "path": "textures/koro_color.png"}, {"name": "koro_eye_iris", "path": "textures/koro_eye.png"}],
        "modern_curves_data_blocks": 0, "volume_data_blocks": 0, "motion_blur": False,
    }
    for key, expected in constants.items():
        common._equal(scene[key], expected, f"{location}.{key}")
    total = sum(common._integer(row["stored_particles"], f"{location}.particle_systems[{index}].stored_particles", minimum=0) for index, row in enumerate(PARTICLE_SYSTEMS))
    common._equal(scene["stored_hair_particles_total"], total, f"{location}.stored_hair_particles_total")
    viewport_total = sum(row["stored_particles"] * row["viewport_children_per_parent"] for row in PARTICLE_SYSTEMS)
    render_total = sum(row["stored_particles"] * row["render_children_per_parent"] for row in PARTICLE_SYSTEMS)
    for index, row in enumerate(PARTICLE_SYSTEMS):
        common._equal(row["configured_viewport_children"], row["stored_particles"] * row["viewport_children_per_parent"], f"{location}.particle_systems[{index}].configured_viewport_children")
        common._equal(row["configured_render_children"], row["stored_particles"] * row["render_children_per_parent"], f"{location}.particle_systems[{index}].configured_render_children")
    common._equal(scene["configured_viewport_children_total"], viewport_total, f"{location}.configured_viewport_children_total")
    common._equal(scene["configured_render_children_total"], render_total, f"{location}.configured_render_children_total")
    change = common._object(scene["frame_1_to_7_draft_change"], f"{location}.frame_1_to_7_draft_change")
    common._exact_keys(change, {"global_agreement", "worst_regional_agreement", "interpretation"}, f"{location}.frame_1_to_7_draft_change")
    common._equal(change["global_agreement"], 0.9705459672294844, f"{location}.frame_1_to_7_draft_change.global_agreement")
    common._equal(change["worst_regional_agreement"], 0.8687117034313725, f"{location}.frame_1_to_7_draft_change.worst_regional_agreement")
    if "not evidence of broad deformation" not in common._string(change["interpretation"], f"{location}.frame_1_to_7_draft_change.interpretation"):
        raise common._error(f"{location}.frame_1_to_7_draft_change.interpretation", "must preserve the limited animation interpretation")
    return total


def _validate_selection(selection: dict[str, Any]) -> list[tuple[Path, str]]:
    location = "provenance.selection_protocol"
    common._exact_keys(selection, {"policy_thresholds_unchanged", "calibration_frames", "sample_counts_screened", "low_resolution_screening_runs", "selected_reason", "full_resolution_calibration", "frozen_samples", "selection_and_measurement_separated", "held_out_attestation", "held_out_rule"}, location)
    common._equal(selection["policy_thresholds_unchanged"], {"global_min": 0.9, "worst_regional_min": 0.85, "worst_32px_microtile_min": 0.7}, f"{location}.policy_thresholds_unchanged")
    common._equal(selection["calibration_frames"], [1, 7], f"{location}.calibration_frames")
    common._equal(selection["sample_counts_screened"], [2, 4, 8], f"{location}.sample_counts_screened")
    if 9 in selection["calibration_frames"]:
        raise common._error(location, "held-out frame appears in calibration")
    expected_rows = [
        (7, [288, 512], {"draft": 2, "verify": 2, "reference": 128}, "5113c9b4f91df2645eeb2459487fcef396bb11ea8b2295326e66a972076f9a65", "repaired"),
        (7, [288, 512], {"draft": 4, "verify": 4, "reference": 128}, "10bfbaed1ff676687baf48e373622e89af8e52961f4be5fda45f11ec8d5e25ae", "accepted_without_repair"),
        (7, [288, 512], {"draft": 8, "verify": 8, "reference": 128}, "6be0a9969203cb3a667ed6195d0433b94a16c62561472909b3c928e79e13e474", "accepted_without_repair"),
        (1, [288, 512], {"draft": 4, "verify": 4, "reference": 128}, "61ab524745c7cc94366bc7b1543c5a84cf84aa8359c3647ea7be309bcd17c8ff", "accepted_without_repair"),
    ]
    rows = common._array(selection["low_resolution_screening_runs"], f"{location}.low_resolution_screening_runs")
    if len(rows) != len(expected_rows):
        raise common._error(f"{location}.low_resolution_screening_runs", "must contain exactly four complete low-resolution screens")
    local: list[tuple[Path, str]] = []
    for index, (row, expected) in enumerate(zip(rows, expected_rows, strict=True)):
        row_location = f"{location}.low_resolution_screening_runs[{index}]"
        row = common._object(row, row_location)
        common._exact_keys(row, {"frame", "resolution_width_by_height", "samples", "receipt_path", "receipt_sha256", "outcome"}, row_location)
        common._equal((row["frame"], row["resolution_width_by_height"], row["samples"], row["receipt_sha256"], row["outcome"]), expected, row_location)
        local.append((_absolute_path(row["receipt_path"], f"{row_location}.receipt_path"), row["receipt_sha256"]))
    common._equal(selection["selected_reason"], "4+4 was the lowest tested no-repair setting and passed on both calibration frames 1 and 7; 2+2 repaired on frame 7, while 8+8 also passed but was not the lowest passing setting.", f"{location}.selected_reason")
    full = common._object(selection["full_resolution_calibration"], f"{location}.full_resolution_calibration")
    full_keys = {"frame", "resolution_width_by_height", "samples", "receipt_path", "receipt_sha256", "baseline_seconds", "product_seconds", "product_gate", "reference_audit", "outcome"}
    common._exact_keys(full, full_keys, f"{location}.full_resolution_calibration")
    expected_full = {"frame": 7, "resolution_width_by_height": RESOLUTION, "samples": {"draft": 4, "verify": 4, "reference": 256}, "receipt_sha256": "113db14d42d842fbe6805e134dce7b2cc3a160b6bf0c4b3c9234a1ae0353f141", "baseline_seconds": 7.656164, "product_seconds": 1.387782, "product_gate": {"global_agreement": 0.974653413, "worst_regional_agreement": 0.877845497, "worst_microtile_agreement": 0.847452002}, "reference_audit": {"global_agreement": 0.97981747, "worst_regional_agreement": 0.909835149, "worst_microtile_agreement": 0.868631281}, "outcome": "accepted_without_repair"}
    for key, value in expected_full.items():
        common._equal(full[key], value, f"{location}.full_resolution_calibration.{key}")
    local.append((_absolute_path(full["receipt_path"], f"{location}.full_resolution_calibration.receipt_path"), full["receipt_sha256"]))
    common._equal(selection["frozen_samples"], {"draft": 4, "verify": 4, "reference": 4096}, f"{location}.frozen_samples")
    common._equal(selection["selection_and_measurement_separated"], True, f"{location}.selection_and_measurement_separated")
    common._equal(selection["held_out_attestation"], "local_operator_declared_unattested", f"{location}.held_out_attestation")
    held_out = common._string(selection["held_out_rule"], f"{location}.held_out_rule")
    for fragment in ("Frame 9", "not rendered", "frozen"):
        if fragment not in held_out:
            raise common._error(f"{location}.held_out_rule", f"missing required fragment {fragment!r}")
    return local


def _validate_provenance(provenance: dict[str, Any], receipt: dict[str, Any], receipt_bytes: bytes, checked: dict[str, Any]) -> dict[str, Any]:
    location = "provenance"
    common._exact_keys(provenance, PROVENANCE_KEYS, location)
    common._equal(provenance["schema_version"], 1, f"{location}.schema_version")
    common._equal(provenance["kind"], PROVENANCE_KIND, f"{location}.kind")
    common._equal(provenance["status"], "accepted_preview_experiment", f"{location}.status")
    common._string(provenance["created_at_utc"], f"{location}.created_at_utc")
    pin = common._object(provenance["receipt"], f"{location}.receipt")
    common._exact_keys(pin, {"path", "sha256", "canonical_json_sha256", "canonical_json_method"}, f"{location}.receipt")
    common._equal(pin["path"], "proof/performance/apple-metal-render-transfer-koro-portrait-2026-07-12.json", f"{location}.receipt.path")
    common._equal(pin["sha256"], _digest(receipt_bytes), f"{location}.receipt.sha256")
    common._equal(pin["canonical_json_sha256"], _digest(_canonical_bytes(receipt)), f"{location}.receipt.canonical_json_sha256")
    common._equal(pin["canonical_json_method"], "Python json.dumps(sort_keys=True,separators=(',',':'),ensure_ascii=False) followed by one LF byte", f"{location}.receipt.canonical_json_method")
    claim = common._object(provenance["claim"], f"{location}.claim")
    common._exact_keys(claim, {"scene_family", "fresh_held_out_frame", "portrait_resolution_width_by_height", "measured_same_session_speedup_x", "baseline_seconds", "product_seconds", "meets_50x_preview_experiment", "meets_100x_preview_experiment", "production_claim_authorized"}, f"{location}.claim")
    expected_claim = {"scene_family": "rigged_character_portrait_with_legacy_particle_hair", "fresh_held_out_frame": 9, "portrait_resolution_width_by_height": RESOLUTION, "measured_same_session_speedup_x": checked["speedup"], "baseline_seconds": checked["baseline"], "product_seconds": checked["product"], "meets_50x_preview_experiment": True, "meets_100x_preview_experiment": False, "production_claim_authorized": False}
    common._equal(claim, expected_claim, f"{location}.claim")
    source = common._object(provenance["source"], f"{location}.source")
    _validate_source(source)
    hair_particles = _validate_scene_audit(common._object(provenance["scene_audit"], f"{location}.scene_audit"))
    calibration = _validate_selection(common._object(provenance["selection_protocol"], f"{location}.selection_protocol"))
    execution = common._object(provenance["execution"], f"{location}.execution")
    execution_keys = {"repository_head", "working_tree_clean", "backend_sha256", "benchmark_harness_sha256", "png_compression", "device", "enabled_device", "resolution_width_by_height", "candidate_profile", "native_integrator", "sample_ranges", "sample_ranges_disjoint", "candidate_phase", "repair_used", "cache_used", "cache_used_false_scope", "warmup_cache_effect", "cold_start_included"}
    common._exact_keys(execution, execution_keys, f"{location}.execution")
    expected_execution = {"repository_head": "d9bff100938874b34322c2a46347d5987598e90e", "working_tree_clean": False, "backend_sha256": PINS["backend_sha256"], "benchmark_harness_sha256": PINS["benchmark_harness_sha256"], "png_compression": 0, "device": "GPU/METAL", "enabled_device": "Apple M3 Ultra (GPU - 60 cores)", "resolution_width_by_height": RESOLUTION, "candidate_profile": "native", "native_integrator": NATIVE_INTEGRATOR, "sample_ranges": SAMPLE_RANGES, "sample_ranges_disjoint": True, "candidate_phase": "draft", "repair_used": False, "cache_used": False, "cache_used_false_scope": "No prior result, artifact, or reference-render output was reused; this does not mean cold Blender, Cycles, Metal kernel, filesystem, or operating-system caches.", "warmup_cache_effect": "The declared uncharged full-candidate warmup populated normal renderer and Metal kernel caches before the measured baseline and candidate work.", "cold_start_included": False}
    common._equal(execution, expected_execution, f"{location}.execution")
    quality = common._object(provenance["quality"], f"{location}.quality")
    quality_keys = {"product_decision_reference_free", "receipt_quality_gate_spec_legacy_incomplete", "authoritative_acceptance_source", "product_gate_passed", "product_global_agreement", "product_worst_regional_agreement", "product_worst_microtile_agreement", "fresh_4096_audit_passed", "global_agreement", "worst_regional_agreement", "worst_microtile_agreement", "regional_tiles", "microtiles"}
    common._exact_keys(quality, quality_keys, f"{location}.quality")
    common._equal(quality["receipt_quality_gate_spec_legacy_incomplete"], "The immutable controller details string g>=0.9,wt>=0.85 omits the schema-v2 worst 32px microtile threshold >=0.70 and is not the full acceptance policy.", f"{location}.quality.receipt_quality_gate_spec_legacy_incomplete")
    common._equal(quality["authoritative_acceptance_source"], "The schema-v2 verification manifest's global, regional, and microtile thresholds, corroborated by verifier recomputation from draft.png and verify.png.", f"{location}.quality.authoritative_acceptance_source")
    product_scores = (quality["product_global_agreement"], quality["product_worst_regional_agreement"], quality["product_worst_microtile_agreement"])
    common._equal(product_scores, (0.974675146, 0.877207789, 0.841625817), f"{location}.quality.product_gate")
    for index, score in enumerate(product_scores):
        common._number(score, f"{location}.quality.product_scores[{index}]", minimum=0.0, maximum=1.0)
    product_passed = product_scores[0] >= common.GLOBAL_MIN and product_scores[1] >= common.REGIONAL_MIN and product_scores[2] >= common.MICROTILE_MIN
    common._equal(quality["product_gate_passed"], product_passed, f"{location}.quality.product_gate_passed")
    common._equal(quality["product_decision_reference_free"], True, f"{location}.quality.product_decision_reference_free")
    common._equal(quality["fresh_4096_audit_passed"], True, f"{location}.quality.fresh_4096_audit_passed")
    common._equal((quality["global_agreement"], quality["worst_regional_agreement"], quality["worst_microtile_agreement"]), (checked["audit"]["global"], checked["audit"]["regional"], checked["audit"]["micro"]), f"{location}.quality.reference_audit")
    common._equal((quality["regional_tiles"], quality["microtiles"]), (144, 1980), f"{location}.quality.tile_counts")
    local = common._object(provenance["local_artifacts"], f"{location}.local_artifacts")
    common._exact_keys(local, {"root", "unit_relative_path", "files"}, f"{location}.local_artifacts")
    _absolute_path(local["root"], f"{location}.local_artifacts.root")
    common._equal(local["root"], receipt["artifact_root"], f"{location}.local_artifacts.root")
    unit = common._relative_path(local["unit_relative_path"], f"{location}.local_artifacts.unit_relative_path")
    common._equal(str(unit / "draft-manifest.json"), checked["output"]["manifest_path"], f"{location}.local_artifacts.unit_relative_path")
    files = common._object(local["files"], f"{location}.local_artifacts.files")
    common._exact_keys(files, LOCAL_FILE_NAMES, f"{location}.local_artifacts.files")
    for name in sorted(files):
        descriptor = common._object(files[name], f"{location}.local_artifacts.files.{name}")
        common._exact_keys(descriptor, {"bytes", "sha256"}, f"{location}.local_artifacts.files.{name}")
        common._integer(descriptor["bytes"], f"{location}.local_artifacts.files.{name}.bytes", minimum=1)
        common._sha256(descriptor["sha256"], f"{location}.local_artifacts.files.{name}.sha256")
    limitations = common._array(provenance["limitations"], f"{location}.limitations")
    if len(limitations) < 8 or any(not isinstance(item, str) or not item for item in limitations):
        raise common._error(f"{location}.limitations", "must contain at least eight non-empty caveats")
    joined = " ".join(limitations)
    for fragment in ("fixed-order", "portrait 1080 by 1920", "legacy particle hair", "modern curves", "not broad deformation", "no variance", "local-unattested", "arbitrary-scene", "legacy and incomplete", "all three verification-manifest thresholds", "not a cold-cache measurement", "local_operator_declared_unattested", "cannot independently prove"):
        if fragment not in joined:
            raise common._error(f"{location}.limitations", f"missing caveat fragment {fragment!r}")
    return {"source": source, "calibration": calibration, "hair_particles": hair_particles, "quality": quality, "local": local}


def _verify_descriptor(path: Path, descriptor: dict[str, Any], location: str, *, maximum: int = common.MAX_ARTIFACT_BYTES) -> bytes:
    data = common._stable_bytes(path, location, maximum=maximum)
    common._equal(len(data), descriptor["bytes"], f"{location}.bytes")
    common._equal(_digest(data), descriptor["sha256"], f"{location}.sha256")
    return data


def _verify_source_files(source: dict[str, Any], *, require_local: bool) -> bool:
    archive = _absolute_path(source["archive_path"], "provenance.source.archive_path")
    root = _absolute_path(source["bundle_root"], "provenance.source.bundle_root")
    archive_present = archive.exists()
    root_present = root.exists()
    if require_local and (not archive_present or not root_present):
        raise common._error("local.source", "required source archive or bundle is absent")
    if archive_present:
        data = common._stable_bytes(archive, "local.source.archive", maximum=128 * 1024 * 1024)
        common._equal(len(data), ARCHIVE_BYTES, "local.source.archive.bytes")
        common._equal(_digest(data), ARCHIVE_SHA256, "local.source.archive.sha256")
    if root_present:
        if root.is_symlink() or not root.is_dir():
            raise common._error("local.source.bundle", "must be a non-symlink directory")
        actual_paths: list[str] = []
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_symlink():
                raise common._error("local.source.bundle", "symlinks are forbidden")
            if path.is_dir():
                continue
            if not path.is_file():
                raise common._error("local.source.bundle", "contains a non-regular entry")
            actual_paths.append(path.relative_to(root).as_posix())
        common._equal(actual_paths, [entry["path"] for entry in BUNDLE_ENTRIES], "local.source.bundle.file_set")
        actual_entries = []
        for expected in BUNDLE_ENTRIES:
            path = common._contained_file(root, expected["path"], f"local.source.bundle.{expected['path']}")
            data = common._stable_bytes(path, f"local.source.bundle.{expected['path']}", maximum=64 * 1024 * 1024)
            actual_entries.append({"path": expected["path"], "bytes": len(data), "sha256": _digest(data)})
        common._equal(actual_entries, BUNDLE_ENTRIES, "local.source.bundle.entries")
        common._equal(_bundle_digest(actual_entries), BUNDLE_SHA256, "local.source.bundle.sha256")
    return archive_present and root_present


def _verify_calibrations(calibrations: list[tuple[Path, str]], *, require_local: bool) -> int:
    count = 0
    for index, (path, expected_sha) in enumerate(calibrations):
        if not path.exists():
            if require_local:
                raise common._error(f"local.calibrations[{index}]", "required calibration receipt is absent")
            continue
        _, data = common._read_json(path, f"local.calibrations[{index}]")
        common._equal(_digest(data), expected_sha, f"local.calibrations[{index}].sha256")
        count += 1
    return count


def _decode_png(data: bytes, location: str) -> Any:
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:  # pragma: no cover - local dependency boundary
        raise common._error(location, "Pillow is required for local image audits") from exc
    try:
        with Image.open(BytesIO(data)) as source:
            if source.format != "PNG" or list(source.size) != RESOLUTION:
                raise common._error(location, "PNG format or portrait dimensions are wrong")
            image = source.convert("RGB")
            image.load()
            return image
    except (OSError, UnidentifiedImageError) as exc:
        raise common._error(location, f"cannot decode PNG: {exc}") from exc


def _agreement(left_bytes: bytes, right_bytes: bytes, location: str) -> tuple[float, float, float]:
    try:
        from PIL import ImageChops, ImageStat
    except ImportError as exc:  # pragma: no cover - local dependency boundary
        raise common._error(location, "Pillow is required for local image audits") from exc
    left = _decode_png(left_bytes, f"{location}.left")
    right = _decode_png(right_bytes, f"{location}.right")
    difference = ImageChops.difference(left, right)
    width, height = RESOLUTION

    def score(image: Any) -> float:
        means = ImageStat.Stat(image).mean
        return min(1.0, max(0.0, 1.0 - sum(means) / (len(means) * 255.0)))

    def minimum(columns: int, rows: int) -> float:
        return min(
            score(difference.crop((column * width // columns, row * height // rows, (column + 1) * width // columns, (row + 1) * height // rows)))
            for row in range(rows)
            for column in range(columns)
        )

    return round(score(difference), 9), round(minimum(9, 16), 9), round(minimum(33, 60), 9)


def _validate_manifest(manifest: dict[str, Any], phase: str, receipt: dict[str, Any], checked: dict[str, Any], unit: PurePosixPath) -> None:
    location = f"local.{phase}_manifest"
    common._exact_keys(manifest, MANIFEST_KEYS, location)
    constants = {"schema_version": 2, "kind": "cx_cycles_preview_manifest", "phase": phase, "artifact_verified": False, "billing_eligible": False, "evidence": "synthetic", "preview_only": True, "production_ready": False, "unit_id": "local-metal-benchmark", "execution_identity_revalidation": {"initial_content": "sha256", "per_render": "pre_and_post_stat_identity_plus_bundle_file_set"}}
    for key, value in constants.items():
        common._equal(manifest[key], value, f"{location}.{key}")
    common._equal(manifest["binding_sha256"], checked["audit"]["binding"], f"{location}.binding_sha256")
    artifact = common._object(manifest["artifact"], f"{location}.artifact")
    common._exact_keys(artifact, {"media_type", "path", "sha256"}, f"{location}.artifact")
    common._equal(artifact["media_type"], "image/png", f"{location}.artifact.media_type")
    common._equal(artifact["path"], str(unit / f"{phase}.png"), f"{location}.artifact.path")
    expected_sha = checked["audit"]["candidate_sha256" if phase == "draft" else "baseline_sha256"]
    common._equal(artifact["sha256"], expected_sha, f"{location}.artifact.sha256")
    pins = common._object(manifest["pins"], f"{location}.pins")
    common._exact_keys(pins, {"backend_sha256", "blender_sha256", "child_script_sha256", "controller_adapter_sha256", "controller_core_sha256"}, f"{location}.pins")
    for key in ("backend_sha256", "blender_sha256", "controller_adapter_sha256", "controller_core_sha256"):
        common._equal(pins[key], receipt["pins"][key], f"{location}.pins.{key}")
    common._equal(pins["child_script_sha256"], "84194e817a8bc4168d45235b9fb3f91cb45ee31586c20956c7e19257737687ce", f"{location}.pins.child_script_sha256")
    scene = common._object(manifest["scene"], f"{location}.scene")
    common._exact_keys(scene, {"bundle_bytes", "bundle_files", "bundle_sha256", "relative_path", "sha256"}, f"{location}.scene")
    common._equal(scene, {"bundle_bytes": BUNDLE_BYTES, "bundle_files": len(BUNDLE_ENTRIES), "bundle_sha256": BUNDLE_SHA256, "relative_path": "main.blend", "sha256": SCENE_SHA256}, f"{location}.scene")
    render = common._object(manifest["render"], f"{location}.render")
    common._exact_keys(render, RENDER_KEYS, f"{location}.render")
    expected_samples = 4 if phase == "draft" else 4096
    expected_offset = 0 if phase == "draft" else 8
    for key, value in {"device": "GPU/METAL", "engine": "CYCLES", "frame": 9, "height": 1920, "png_compression": 0, "sample_offset": expected_offset, "samples": expected_samples, "width": 1080}.items():
        common._equal(render[key], value, f"{location}.render.{key}")
    common._integer(render["seed"], f"{location}.render.seed", minimum=0)
    common._equal(render["pixel_filter"], {"type": "BLACKMAN_HARRIS", "width": 1.5}, f"{location}.render.pixel_filter")
    _validate_worker(render["worker_renderer_identity"], f"{location}.render.worker_renderer_identity")
    policy = common._object(render["integrator_policy"], f"{location}.render.integrator_policy")
    common._exact_keys(policy, {"actual_integrator", "actual_sampling", "candidate_profile", "candidate_profile_scope", "mode", "repair_and_baseline_use_reference_policy", "samples_are_cap_when_adaptive"}, f"{location}.render.integrator_policy")
    common._equal(policy, {"actual_integrator": NATIVE_INTEGRATOR, "actual_sampling": REFERENCE_SAMPLING, "candidate_profile": NATIVE_PROFILE, "candidate_profile_scope": "benchmark_screen_v1", "mode": "fixed_reference", "repair_and_baseline_use_reference_policy": True, "samples_are_cap_when_adaptive": False}, f"{location}.render.integrator_policy")


def _verify_local_artifacts(receipt: dict[str, Any], provenance_checked: dict[str, Any], checked: dict[str, Any], *, require_local: bool) -> bool:
    local = provenance_checked["local"]
    root = _absolute_path(local["root"], "provenance.local_artifacts.root")
    if not root.exists():
        if require_local:
            raise common._error("local.artifacts", "required local artifact root is absent")
        return False
    if root.is_symlink() or not root.is_dir():
        raise common._error("local.artifacts", "artifact root must be a non-symlink directory")
    unit = common._relative_path(local["unit_relative_path"], "provenance.local_artifacts.unit_relative_path")
    contents: dict[str, bytes] = {}
    parsed: dict[str, dict[str, Any]] = {}
    for name in sorted(LOCAL_FILE_NAMES):
        path = common._contained_file(root, str(unit / name), f"local.{name}.path")
        contents[name] = _verify_descriptor(path, local["files"][name], f"local.{name}")
        if name.endswith(".json"):
            parsed[name] = common._strict_json_bytes(contents[name], f"local.{name}")
    _validate_manifest(parsed["draft-manifest.json"], "draft", receipt, checked, unit)
    _validate_manifest(parsed["baseline-manifest.json"], "baseline", receipt, checked, unit)
    verification = parsed["verification-manifest.json"]
    common._exact_keys(verification, common.VERIFICATION_KEYS, "local.verification_manifest")
    verification_constants = {"schema_version": 2, "kind": "cx_cycles_preview_verification", "accepted": True, "artifact_verified": False, "billing_eligible": False, "evidence": "synthetic", "failing_tile_count": 0, "failing_tiles": [], "failing_tiles_truncated": False, "global_min": 0.9, "independent_seed": True, "metric": common.QUALITY_METRIC, "microtile_contract": "fixed_scale_catastrophic_defect_sentinel", "microtile_count": 1980, "microtile_grid": {"columns": 33, "nominal_edge_pixels": 32, "rows": 60}, "preview_only": True, "production_ready": False, "repair_plan": None, "sample_ranges_disjoint": True, "selected_manifest_path": str(unit / "draft-manifest.json"), "tile_contract": "resolution_relative_regions_not_fixed_pixel_defects", "tile_count": 144, "tile_grid": {"columns": 9, "max_long_edge_tiles": 16, "minimum_nominal_edge_pixels": 32, "rows": 16}, "unit_id": "local-metal-benchmark", "worst_microtile_min": 0.7, "worst_tile_min": 0.85, "draft_sample_offset": 0, "verify_sample_offset": 4}
    for key, value in verification_constants.items():
        common._equal(verification[key], value, f"local.verification_manifest.{key}")
    common._equal(verification["binding_sha256"], checked["audit"]["binding"], "local.verification_manifest.binding_sha256")
    common._integer(verification["draft_seed"], "local.verification_manifest.draft_seed", minimum=0)
    common._integer(verification["verify_seed"], "local.verification_manifest.verify_seed", minimum=0)
    if verification["draft_seed"] == verification["verify_seed"]:
        raise common._error("local.verification_manifest", "draft and verify seeds are not independent")
    verify_artifact = common._object(verification["verify_artifact"], "local.verification_manifest.verify_artifact")
    common._exact_keys(verify_artifact, {"path", "sha256"}, "local.verification_manifest.verify_artifact")
    common._equal(verify_artifact, {"path": str(unit / "verify.png"), "sha256": local["files"]["verify.png"]["sha256"]}, "local.verification_manifest.verify_artifact")
    audit = parsed["benchmark-audit.json"]
    common._exact_keys(audit, common.AUDIT_KEYS, "local.benchmark_audit")
    common._equal(audit, receipt["benchmark_audit"], "local.benchmark_audit")
    product_expected = (provenance_checked["quality"]["product_global_agreement"], provenance_checked["quality"]["product_worst_regional_agreement"], provenance_checked["quality"]["product_worst_microtile_agreement"])
    common._equal((verification["global_agreement"], verification["worst_tile_agreement"], verification["worst_microtile_agreement"]), product_expected, "local.verification_manifest.product_gate")
    product_actual = _agreement(contents["draft.png"], contents["verify.png"], "local.product_gate")
    common._equal(product_actual, product_expected, "local.product_gate")
    audit_expected = (checked["audit"]["global"], checked["audit"]["regional"], checked["audit"]["micro"])
    audit_actual = _agreement(contents["draft.png"], contents["baseline.png"], "local.reference_audit")
    common._equal(audit_actual, audit_expected, "local.reference_audit")
    return True


def verify(receipt_path: Path = DEFAULT_RECEIPT, provenance_path: Path = DEFAULT_PROVENANCE, *, require_local_artifacts: bool = False) -> dict[str, Any]:
    receipt, receipt_bytes = common._read_json(receipt_path, "receipt")
    provenance, provenance_bytes = common._read_json(provenance_path, "provenance")
    common._equal(_digest(receipt_bytes), RECEIPT_RAW_SHA256, "receipt.sha256")
    common._equal(
        _digest(_canonical_bytes(receipt)),
        RECEIPT_CANONICAL_SHA256,
        "receipt.canonical_json_sha256",
    )
    checked = _validate_receipt(receipt)
    provenance_checked = _validate_provenance(provenance, receipt, receipt_bytes, checked)
    source_verified = _verify_source_files(provenance_checked["source"], require_local=require_local_artifacts)
    calibration_count = _verify_calibrations(provenance_checked["calibration"], require_local=require_local_artifacts)
    artifacts_verified = _verify_local_artifacts(receipt, provenance_checked, checked, require_local=require_local_artifacts)
    return {
        "schema_version": 1,
        "kind": RESULT_KIND,
        "valid": True,
        "receipt_sha256": _digest(receipt_bytes),
        "provenance_sha256": _digest(provenance_bytes),
        "speedup_x": checked["speedup"],
        "meets_50x_preview_experiment": True,
        "stored_hair_particles_audited": provenance_checked["hair_particles"],
        "configured_viewport_children_audited": 226080,
        "configured_render_children_audited": 806530,
        "source_bundle_verified": source_verified,
        "calibration_receipts_verified": calibration_count,
        "local_artifacts_verified": artifacts_verified,
        "product_gate_recomputed": artifacts_verified,
        "reference_audit_recomputed": artifacts_verified,
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
        result = verify(args.receipt, args.provenance, require_local_artifacts=args.require_local_artifacts)
    except Exception as exc:  # noqa: BLE001 - one fail-closed CLI envelope
        print(json.dumps({"schema_version": 1, "kind": RESULT_KIND, "valid": False, "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
