#!/usr/bin/env python3
"""Adversarial tests for the static-frame cache reuse screen."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import screen_static_frame_cache_reuse as cache  # noqa: E402


SHA = "a" * 64


def artifact(path: str, digest: str = SHA) -> dict[str, object]:
    return {"path": path, "bytes": 1, "sha256": digest}


def publish_fixture(root: Path, relative: str, data: bytes) -> dict[str, object]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {
        "path": relative,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def quality_fixture(
    candidate: dict[str, object], reference: dict[str, object]
) -> tuple[dict[str, object], dict[str, object]]:
    metrics = {
        "global_rgb_agreement": {"value": 1.0},
        "worst_regional_rgb_agreement": {"value": 1.0},
        "worst_microtile_rgb_agreement": {"value": 1.0},
        "gaussian_luma_ssim": {"value": 1.0},
        "sobel_gms_mean": {"value": 1.0},
    }
    proof = {
        "schema_version": 3,
        "kind": cache.QUALITY_KIND,
        "pass": True,
        "failures": [],
        "errors": [],
        "inputs": {
            "target_dimensions": list(cache.DIMENSIONS),
            "candidate": {**candidate, "mode": "RGBA"},
            "reference": {**reference, "mode": "RGBA"},
        },
        "alpha_agreement": {"value": 1.0},
        "mattes": {
            "black": {"metrics": metrics},
            "white": {"metrics": metrics},
        },
        "contract_sha256": cache.QUALITY_V3_CONTRACT_SHA256,
        "runtime": {
            "metric_module_sha256": cache.implementation_pins()[
                "quality_v3_module_sha256"
            ]
        },
    }
    proof_sha = hashlib.sha256(cache.canonical_json(proof)).hexdigest()
    verification = {
        "schema_version": 1,
        "kind": cache.VERIFICATION_KIND,
        "pass": True,
        "proof_verified": True,
        "quality_pass": True,
        "errors": [],
        "artifacts": {
            "candidate": {
                "bytes": candidate["bytes"],
                "sha256": candidate["sha256"],
            },
            "reference": {
                "bytes": reference["bytes"],
                "sha256": reference["sha256"],
            },
        },
        "proof_result_sha256": proof_sha,
        "recomputed_result_sha256": proof_sha,
        "verifier": "cx-render-preview-quality-v3-verifier-v1",
    }
    return proof, verification


def request_identity() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "cx_static_frame_exact_request_identity",
        "manifest_binding_sha256": "1" * 64,
        "unit_id": "unit",
        "phase": "baseline",
        "frame": 9,
        "scene_bundle": {
            "bytes": 100,
            "files": 2,
            "sha256": "2" * 64,
            "main_blend_sha256": "3" * 64,
            "relative_path": "main.blend",
        },
        "render_recipe_and_policy": {
            "frame": 9,
            "samples": cache.REFERENCE_SAMPLES,
            "width": cache.DIMENSIONS[0],
            "height": cache.DIMENSIONS[1],
            "engine": "CYCLES",
            "policy": {"mode": "fixed_reference"},
            "worker_renderer_identity": {"version": "4.2.1"},
        },
        "runtime_and_implementation_pins": {
            "backend_sha256": "4" * 64,
            "blender_sha256": "5" * 64,
            "child_script_sha256": "6" * 64,
            "controller_adapter_sha256": "7" * 64,
            "controller_core_sha256": "8" * 64,
        },
        "expected_output_contract": {
            "media_type": "image/png",
            "width": cache.DIMENSIONS[0],
            "height": cache.DIMENSIONS[1],
            "mode": "RGBA",
            "png_compression": 0,
            "samples": cache.REFERENCE_SAMPLES,
            "engine": "CYCLES",
            "device": "GPU/METAL",
            "preview_only": True,
            "production_ready": False,
            "artifact_verified": False,
            "billing_eligible": False,
        },
    }


def make_index(root: Path, payload: bytes) -> tuple[Path, dict[str, object], str]:
    source = root / "cache" / "source.bin"
    source.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    artifact = {
        "path": source.name,
        "bytes": len(payload),
        "sha256": digest,
        "dimensions": list(cache.DIMENSIONS),
        "mode": "RGBA",
        "samples": cache.REFERENCE_SAMPLES,
        "source_frame": 9,
    }
    manifest = {
        "path": "source-manifest.json",
        "bytes": 2,
        "sha256": hashlib.sha256(b"{}" ).hexdigest(),
    }
    scene = {
        "bundle_bytes": 100,
        "bundle_files": 2,
        "bundle_sha256": "2" * 64,
        "main_blend_sha256": "3" * 64,
    }
    index = cache.build_cache_index(
        artifact=artifact,
        source_manifest=manifest,
        serialized_scene_identity=scene,
        request_identity=request_identity(),
    )
    index_path = root / "cache" / "index.json"
    index_data = cache.canonical_json(index) + b"\n"
    index_path.write_bytes(index_data)
    return index_path, index, hashlib.sha256(index_data).hexdigest()


def synthetic_receipt() -> dict[str, object]:
    import statistics

    identity = request_identity()
    eligibility = {
        "preview_only": True,
        "production_ready": False,
        "artifact_verified": False,
        "billing_eligible": False,
    }
    source_artifact = artifact("cache/source.png", "c" * 64)
    scene = {
        "bundle_bytes": 100,
        "bundle_files": 2,
        "bundle_sha256": "2" * 64,
        "main_blend_sha256": "3" * 64,
    }
    recipe = {"engine": "CYCLES", "samples": cache.REFERENCE_SAMPLES}
    target_10 = artifact("evidence/frame-10.png", "d" * 64)
    target_11 = artifact("evidence/frame-11.png", "e" * 64)
    targets = {
        "frame_10": {
            "frame": 10,
            "baseline_s": 112.0,
            "reference": target_10,
            "frontier_receipt": artifact("evidence/frame-10-receipt.json"),
            "baseline_manifest": artifact("evidence/frame-10-manifest.json"),
            "serialized_scene_identity": scene,
            "render_recipe_excluding_frame_and_seed": recipe,
        },
        "frame_11": {
            "frame": 11,
            "baseline_s": 113.0,
            "reference": target_11,
            "frontier_receipt": artifact("evidence/frame-11-receipt.json"),
            "baseline_manifest": artifact("evidence/frame-11-manifest.json"),
            "serialized_scene_identity": scene,
            "render_recipe_excluding_frame_and_seed": recipe,
        },
    }
    samples = [0.010, 0.011, 0.012]
    trials = []
    for index, sample in enumerate(samples):
        total = round(sample * 1_000_000_000)
        trials.append(
            {
                "trial_index": index,
                "total_s": sample,
                "timings_ns": {
                    "cache_lookup": 1_000_000,
                    "full_sha256_validation": 1_000_000,
                    "durable_artifact_publication": 1_000_000,
                    "durable_sidecar_receipt": 1_000_000,
                    "total": total,
                },
                "publication_mode": "copy",
                "delivery": artifact(f"trials/{index}-delivery.png", "c" * 64),
                "sidecar_receipt": artifact(f"trials/{index}-receipt.json"),
            }
        )
    median = statistics.median(samples)
    baseline = 112.396726
    storage = (
        "same_uid_storage_writers_are_trusted_not_to_mutate_during_the_"
        "transaction_or_after_return_and_each_consumption_is_fully_sha256_"
        "validated"
    )
    quality_identity = {
        "equivalence": "sha256_exact_bytes",
        "source_sha256": "c" * 64,
        "source_bytes": 1,
        "all_trial_deliveries_byte_identical": True,
    }
    audits = {
        "frame_9_vs_frame_10": {
            "candidate_frame": 9,
            "reference_frame": 10,
            "candidate": source_artifact,
            "reference": target_10,
            "pass": True,
            "proof_verified": True,
            "proof": artifact("evidence/9-10-proof.json", "1" * 64),
            "verification": artifact("evidence/9-10-verify.json", "4" * 64),
            "summary": {"global": 1.0},
        },
        "frame_9_vs_frame_11": {
            "candidate_frame": 9,
            "reference_frame": 11,
            "candidate": source_artifact,
            "reference": target_11,
            "pass": True,
            "proof_verified": True,
            "proof": artifact("evidence/9-11-proof.json", "2" * 64),
            "verification": artifact("evidence/9-11-verify.json", "5" * 64),
            "summary": {"global": 1.0},
        },
        "frame_10_vs_frame_11": {
            "candidate_frame": 10,
            "reference_frame": 11,
            "candidate": target_10,
            "reference": target_11,
            "pass": True,
            "proof_verified": True,
            "proof": artifact("evidence/10-11-proof.json", "3" * 64),
            "verification": artifact("evidence/10-11-verify.json", "6" * 64),
            "summary": {"global": 1.0},
        },
    }
    return {
        "schema_version": cache.SCHEMA_VERSION,
        "kind": cache.RECEIPT_KIND,
        "evidence": "synthetic",
        "scope_partitioned": True,
        "claim_scope": "fixture",
        "timing_scope": "fixture",
        "authorization": {
            "exact_transport": {
                "artifact_eligibility_inherited": True,
                "byte_identity_required": True,
                "cross_frame": False,
                "transport_authorized": True,
                "query_identity": (
                    "exact_frame_scene_recipe_policy_runtime_and_output_contract"
                ),
                "storage_trust_assumption": storage,
                "transport": "descriptor_copy_hash_fsync_no_clobber",
            },
            "cross_frame_audit": {
                "cache_selection": "posthoc_quality_v3_known_frames",
                "cross_frame_generalization_authorized": False,
                "production_authorizable": False,
                "product_decision_reference_free": False,
                "reference_used_for_audit": True,
            },
        },
        "pins": cache.implementation_pins(),
        "source": {
            "frame": 9,
            "samples": cache.REFERENCE_SAMPLES,
            "dimensions": list(cache.DIMENSIONS),
            "cache_key": "f" * 64,
            "request_identity": identity,
            "eligibility": eligibility,
            "artifact": source_artifact,
            "serialized_scene_identity": scene,
        },
        "fingerprint_assessment": cache.assess_serialized_fingerprint(
            [scene, scene, scene], [recipe, recipe, recipe]
        ),
        "quality_audits": audits,
        "measurement": {
            "trial_count": 3,
            "samples_s": samples,
            "median_s": median,
            "p95_s_type7": cache._type7_quantile(samples, 0.95),
            "minimum_s": min(samples),
            "maximum_s": max(samples),
            "population_variance_s2": statistics.pvariance(samples),
            "included_stages": [
                "strict_cache_index_lookup",
                "full_cached_artifact_sha256_validation",
                "durable_no_clobber_delivery_publication",
                "durable_bound_sidecar_receipt_publication",
            ],
            "excluded_from_headline": [],
            "headline_scopes": {
                "exact_transport": {
                    "scope": (
                        "exact_request_byte_transport_with_inherited_source_eligibility"
                    ),
                    "transport_authorized": True,
                    "artifact_eligibility_inherited": True,
                    "source_eligibility": eligibility,
                    "concrete_artifact_production_eligible": False,
                    "concrete_artifact_billing_eligible": False,
                    "cross_frame": False,
                    "transport": "descriptor_copy_hash_fsync_no_clobber",
                    "storage_trust_assumption": storage,
                    "request_cache_key": "f" * 64,
                    "baseline_s": baseline,
                    "median_speedup_x": baseline / median,
                    "p95_s_type7": cache._type7_quantile(samples, 0.95),
                    "slowest_s": max(samples),
                    "slowest_speedup_x": baseline / max(samples),
                    "per_trial_1000x_latency_ceiling_s": baseline / 1000.0,
                    "all_9_trials_exceed_1000x": False,
                    "quality_identity": quality_identity,
                },
                "cross_frame_audit": {
                    "scope": "posthoc_approximate_cross_frame_quality_v3_audit",
                    "audit_only": True,
                    "production_authorizable": False,
                    "independent_quality_v3_verified": True,
                    "target_frames": [10, 11],
                    "baselines_s": {"frame_10": 112.0, "frame_11": 113.0},
                    "median_speedup_x": {
                        "frame_10": 112.0 / median,
                        "frame_11": 113.0 / median,
                    },
                    "median_exceeds_1000x_on_all_targets": True,
                },
            },
            "trials": trials,
        },
        "artifacts": {
            "cache_index": artifact("cache/index.json"),
            "cache_source": source_artifact,
            "source_manifest": artifact("cache/source-manifest.json"),
            "source_performance_receipt": artifact("evidence/performance.json"),
            "targets": targets,
        },
        "host": {},
    }


class StaticFrameCacheReuseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "cache").mkdir()
        (self.root / "trials").mkdir()
        self.payload = (b"closed-cache-payload" * 65536) + b"!"

    def test_source_eligibility_is_bound_and_never_upgraded(self) -> None:
        identity = request_identity()
        scene = identity["scene_bundle"]
        manifest = {
            "binding_sha256": identity["manifest_binding_sha256"],
            "unit_id": identity["unit_id"],
            "phase": "baseline",
            "render": identity["render_recipe_and_policy"],
            "scene": {
                "bundle_bytes": scene["bytes"],
                "bundle_files": scene["files"],
                "bundle_sha256": scene["sha256"],
                "sha256": scene["main_blend_sha256"],
                "relative_path": scene["relative_path"],
            },
            "pins": identity["runtime_and_implementation_pins"],
            "artifact": {"media_type": "image/png"},
            "preview_only": True,
            "production_ready": False,
            "artifact_verified": False,
            "billing_eligible": False,
        }
        eligibility = cache.source_eligibility_from_manifest(manifest)
        projected = cache.request_identity_from_manifest(manifest)
        self.assertEqual(
            eligibility,
            {
                "preview_only": True,
                "production_ready": False,
                "artifact_verified": False,
                "billing_eligible": False,
            },
        )
        for key, value in eligibility.items():
            self.assertIs(projected["expected_output_contract"][key], value)

    def test_request_key_is_request_derived_not_artifact_derived(self) -> None:
        first_path, first, _ = make_index(self.root, self.payload)
        first_path.unlink()
        changed_artifact = copy.deepcopy(first["artifact"])
        changed_artifact["sha256"] = "9" * 64
        changed_artifact["bytes"] += 1
        second = cache.build_cache_index(
            artifact=changed_artifact,
            source_manifest=first["source_manifest"],
            serialized_scene_identity=first["serialized_scene_identity"],
            request_identity=first["request_identity"],
        )
        self.assertEqual(first["cache_key"], second["cache_key"])
        changed_request = copy.deepcopy(first["request_identity"])
        changed_request["runtime_and_implementation_pins"][
            "backend_sha256"
        ] = "b" * 64
        third = cache.build_cache_index(
            artifact=first["artifact"],
            source_manifest=first["source_manifest"],
            serialized_scene_identity=first["serialized_scene_identity"],
            request_identity=changed_request,
        )
        self.assertNotEqual(first["cache_key"], third["cache_key"])

    def test_descriptor_copy_closes_lookup_hash_publish_and_receipt(self) -> None:
        index_path, index, index_sha = make_index(self.root, self.payload)
        trial = cache.run_timed_trial(
            index_path=index_path,
            index_sha256=index_sha,
            cache_key=index["cache_key"],
            trials_root=self.root / "trials",
            screen_root=self.root,
            trial_index=0,
            prefer_hardlink=False,
        )
        source = self.root / "cache" / "source.bin"
        delivery = self.root / trial["delivery"]["path"]
        self.assertEqual(delivery.read_bytes(), self.payload)
        self.assertNotEqual(source.stat().st_ino, delivery.stat().st_ino)
        self.assertEqual(trial["publication_mode"], "copy")
        self.assertEqual(trial["delivery"]["sha256"], index["artifact"]["sha256"])
        self.assertTrue(all(value > 0 for value in trial["timings_ns"].values()))
        sidecar = json.loads(
            (self.root / trial["sidecar_receipt"]["path"]).read_text()
        )
        self.assertEqual(
            sidecar["authorization"],
            {
                "exact_transport_authorized": True,
                "cross_frame_reuse": False,
                "source_artifact_eligibility_inherited": True,
            },
        )
        self.assertEqual(sidecar["output"]["sha256"], index["artifact"]["sha256"])

    def test_tampered_cache_source_rejects_before_publication(self) -> None:
        index_path, index, index_sha = make_index(self.root, self.payload)
        (self.root / "cache" / "source.bin").write_bytes(b"x" * len(self.payload))
        with self.assertRaisesRegex(cache.CacheScreenError, "SHA-256"):
            cache.run_timed_trial(
                index_path=index_path,
                index_sha256=index_sha,
                cache_key=index["cache_key"],
                trials_root=self.root / "trials",
                screen_root=self.root,
                trial_index=0,
                prefer_hardlink=False,
            )
        self.assertFalse((self.root / "trials" / "trial-00-delivery.png").exists())
        self.assertFalse((self.root / "trials" / "trial-00-receipt.json").exists())

    def test_copy_stage_path_swap_is_detected_and_unpublished(self) -> None:
        index_path, index, index_sha = make_index(self.root, self.payload)
        real_link = os.link

        def swap_stage(source, destination, *, follow_symlinks=True):
            source_path = Path(source)
            stolen = source_path.with_name(source_path.name + ".stolen")
            source_path.rename(stolen)
            source_path.write_bytes(b"z" * len(self.payload))
            return real_link(
                source_path,
                destination,
                follow_symlinks=follow_symlinks,
            )

        with mock.patch.object(os, "link", side_effect=swap_stage):
            with self.assertRaisesRegex(cache.CacheScreenError, "stage identity"):
                cache.run_timed_trial(
                    index_path=index_path,
                    index_sha256=index_sha,
                    cache_key=index["cache_key"],
                    trials_root=self.root / "trials",
                    screen_root=self.root,
                    trial_index=0,
                    prefer_hardlink=False,
                )
        self.assertFalse((self.root / "trials" / "trial-00-delivery.png").exists())
        self.assertFalse((self.root / "trials" / "trial-00-receipt.json").exists())

    def test_sidecar_stage_path_swap_is_detected_and_unpublished(self) -> None:
        index_path, index, index_sha = make_index(self.root, self.payload)
        real_link = os.link
        sidecar = self.root / "trials" / "trial-00-receipt.json"

        def swap_sidecar_stage(source, destination, *, follow_symlinks=True):
            source_path = Path(source)
            if Path(destination) == sidecar:
                stolen = source_path.with_name(source_path.name + ".stolen")
                source_path.rename(stolen)
                source_path.write_bytes(b'{"substituted":true}\n')
            return real_link(
                source_path,
                destination,
                follow_symlinks=follow_symlinks,
            )

        with mock.patch.object(os, "link", side_effect=swap_sidecar_stage):
            with self.assertRaisesRegex(cache.CacheScreenError, "stage identity"):
                cache.run_timed_trial(
                    index_path=index_path,
                    index_sha256=index_sha,
                    cache_key=index["cache_key"],
                    trials_root=self.root / "trials",
                    screen_root=self.root,
                    trial_index=0,
                    prefer_hardlink=False,
                )
        self.assertFalse(sidecar.exists())
        self.assertFalse(
            (self.root / "trials" / "trial-00-delivery.png").exists()
        )

    def test_generic_destination_swap_during_directory_fsync_is_rejected(
        self,
    ) -> None:
        destination = self.root / "trials" / "published.json"
        real_fsync_directory = cache._fsync_directory
        replaced = False

        def replace_after_fsync(parent: Path) -> None:
            nonlocal replaced
            real_fsync_directory(parent)
            if not replaced and destination.exists():
                destination.unlink()
                destination.write_bytes(b"attacker bytes")
                replaced = True

        with mock.patch.object(
            cache, "_fsync_directory", side_effect=replace_after_fsync
        ):
            with self.assertRaisesRegex(
                cache.CacheScreenError, "final published bytes identity"
            ):
                cache._publish_bytes_new(destination, b"expected bytes\n")
        self.assertTrue(replaced)
        self.assertFalse(destination.exists())

    def test_delivery_swap_during_directory_fsync_is_rejected(self) -> None:
        index_path, index, index_sha = make_index(self.root, self.payload)
        delivery = self.root / "trials" / "trial-00-delivery.png"
        real_fsync_directory = cache._fsync_directory
        replaced = False

        def replace_after_fsync(parent: Path) -> None:
            nonlocal replaced
            real_fsync_directory(parent)
            if not replaced and delivery.exists():
                delivery.unlink()
                delivery.write_bytes(b"z" * len(self.payload))
                replaced = True

        with mock.patch.object(
            cache, "_fsync_directory", side_effect=replace_after_fsync
        ):
            with self.assertRaisesRegex(
                cache.CacheScreenError, "final published delivery identity"
            ):
                cache.run_timed_trial(
                    index_path=index_path,
                    index_sha256=index_sha,
                    cache_key=index["cache_key"],
                    trials_root=self.root / "trials",
                    screen_root=self.root,
                    trial_index=0,
                    prefer_hardlink=False,
                )
        self.assertTrue(replaced)
        self.assertFalse(delivery.exists())
        self.assertFalse((self.root / "trials" / "trial-00-receipt.json").exists())

    def test_no_clobber_preserves_existing_destination(self) -> None:
        index_path, index, index_sha = make_index(self.root, self.payload)
        destination = self.root / "trials" / "trial-00-delivery.png"
        destination.write_bytes(b"owner-data")
        with self.assertRaisesRegex(cache.CacheScreenError, "already exists"):
            cache.run_timed_trial(
                index_path=index_path,
                index_sha256=index_sha,
                cache_key=index["cache_key"],
                trials_root=self.root / "trials",
                screen_root=self.root,
                trial_index=0,
                prefer_hardlink=False,
            )
        self.assertEqual(destination.read_bytes(), b"owner-data")

    def test_symlinked_index_or_source_is_rejected(self) -> None:
        index_path, index, index_sha = make_index(self.root, self.payload)
        real_index = self.root / "cache" / "real-index.json"
        index_path.rename(real_index)
        index_path.symlink_to(real_index)
        with self.assertRaisesRegex(cache.CacheScreenError, "symlink"):
            cache.run_timed_trial(
                index_path=index_path,
                index_sha256=index_sha,
                cache_key=index["cache_key"],
                trials_root=self.root / "trials",
                screen_root=self.root,
                trial_index=0,
                prefer_hardlink=False,
            )
        index_path.unlink()
        real_index.rename(index_path)
        source = self.root / "cache" / "source.bin"
        real_source = self.root / "cache" / "real-source.bin"
        source.rename(real_source)
        source.symlink_to(real_source)
        with self.assertRaisesRegex(cache.CacheScreenError, "symlink"):
            cache.run_timed_trial(
                index_path=index_path,
                index_sha256=index_sha,
                cache_key=index["cache_key"],
                trials_root=self.root / "trials",
                screen_root=self.root,
                trial_index=0,
                prefer_hardlink=False,
            )

    def test_cross_frame_fingerprint_never_inherits_exact_authorization(self) -> None:
        scene = {
            "bundle_bytes": 100,
            "bundle_files": 2,
            "bundle_sha256": "2" * 64,
            "main_blend_sha256": "3" * 64,
        }
        recipe = {"engine": "CYCLES", "samples": cache.REFERENCE_SAMPLES}
        result = cache.assess_serialized_fingerprint(
            [scene, copy.deepcopy(scene), copy.deepcopy(scene)],
            [recipe, copy.deepcopy(recipe), copy.deepcopy(recipe)],
        )
        self.assertTrue(result["serialized_inputs_equal"])
        self.assertTrue(result["render_recipes_equal_excluding_frame_and_seed"])
        self.assertFalse(result["cross_frame_reuse_authorized"])
        self.assertFalse(result["reference_free"])
        self.assertIn("insufficient", result["status"])

    def test_aggregate_cache_source_tuple_rejects_every_forged_component(
        self,
    ) -> None:
        _index_path, index, _index_sha = make_index(self.root, self.payload)
        source_artifact = {
            "path": "cache/source.bin",
            "bytes": index["artifact"]["bytes"],
            "sha256": index["artifact"]["sha256"],
        }
        source_manifest_record = {
            "path": "cache/source-manifest.json",
            "bytes": index["source_manifest"]["bytes"],
            "sha256": index["source_manifest"]["sha256"],
        }
        source = {
            "frame": 9,
            "samples": cache.REFERENCE_SAMPLES,
            "dimensions": list(cache.DIMENSIONS),
            "request_identity": copy.deepcopy(index["request_identity"]),
            "artifact": copy.deepcopy(source_artifact),
            "serialized_scene_identity": copy.deepcopy(
                index["serialized_scene_identity"]
            ),
        }
        valid = {
            "index": copy.deepcopy(index),
            "source": source,
            "cache_source_record": copy.deepcopy(source_artifact),
            "source_manifest_record": source_manifest_record,
            "cache_source_basename": "source.bin",
            "source_manifest_basename": "source-manifest.json",
            "manifest_scene": copy.deepcopy(index["serialized_scene_identity"]),
        }

        def validate(context: dict[str, object]) -> None:
            cache._validate_aggregate_cache_source_tuple(**context)

        def forged(value: object) -> object:
            if type(value) is int:
                return value + 1
            if isinstance(value, list):
                return [value[0] + 1, *value[1:]]
            if isinstance(value, str) and len(value) == 64:
                return ("9" if value[0] != "9" else "8") * 64
            if isinstance(value, str):
                return f"{value}.forged"
            self.fail(f"no forgery transform for {value!r}")

        def change_path(
            context: dict[str, object], path: tuple[str, ...]
        ) -> None:
            current: object = context
            for key in path[:-1]:
                current = current[key]  # type: ignore[index]
            leaf = path[-1]
            current[leaf] = forged(current[leaf])  # type: ignore[index]

        validate(copy.deepcopy(valid))
        paths: list[tuple[str, ...]] = []
        paths.extend(
            ("index", "artifact", key)
            for key in (
                "path",
                "bytes",
                "sha256",
                "dimensions",
                "mode",
                "samples",
                "source_frame",
            )
        )
        paths.extend(
            ("source", "artifact", key) for key in ("path", "bytes", "sha256")
        )
        paths.extend(
            ("cache_source_record", key) for key in ("path", "bytes", "sha256")
        )
        paths.extend(
            ("index", "source_manifest", key)
            for key in ("path", "bytes", "sha256")
        )
        paths.extend(
            ("source_manifest_record", key)
            for key in ("path", "bytes", "sha256")
        )
        paths.extend(
            ("source", key) for key in ("frame", "samples", "dimensions")
        )
        paths.append(
            (
                "source",
                "request_identity",
                "expected_output_contract",
                "mode",
            )
        )
        for container in (
            ("index", "serialized_scene_identity"),
            ("source", "serialized_scene_identity"),
            ("manifest_scene",),
        ):
            paths.extend(
                (*container, key)
                for key in (
                    "bundle_bytes",
                    "bundle_files",
                    "bundle_sha256",
                    "main_blend_sha256",
                )
            )
        for key in ("bytes", "files", "sha256", "main_blend_sha256"):
            paths.append(("source", "request_identity", "scene_bundle", key))
        paths.extend(
            (key,)
            for key in ("cache_source_basename", "source_manifest_basename")
        )

        for path in paths:
            with self.subTest(component=".".join(path)):
                changed = copy.deepcopy(valid)
                change_path(changed, path)
                with self.assertRaises(cache.CacheScreenError):
                    validate(changed)

    def test_source_performance_denominator_is_bound_to_cached_render(self) -> None:
        identity = request_identity()
        identity["render_recipe_and_policy"]["device"] = "GPU/METAL"
        source = {
            "frame": 9,
            "samples": cache.REFERENCE_SAMPLES,
            "dimensions": list(cache.DIMENSIONS),
            "request_identity": copy.deepcopy(identity),
            "eligibility": {
                "preview_only": True,
                "production_ready": False,
                "artifact_verified": False,
                "billing_eligible": False,
            },
            "artifact": {
                "path": "cache/source.png",
                "bytes": 123,
                "sha256": "c" * 64,
            },
            "serialized_scene_identity": {
                "bundle_bytes": 100,
                "bundle_files": 2,
                "bundle_sha256": "2" * 64,
                "main_blend_sha256": "3" * 64,
            },
        }
        source_manifest = {
            "binding_sha256": identity["manifest_binding_sha256"],
            "artifact": {
                "path": "unit/baseline.png",
                "sha256": "c" * 64,
            },
            "scene": {"sha256": "3" * 64},
            "render": {
                "worker_renderer_identity": copy.deepcopy(
                    identity["render_recipe_and_policy"][
                        "worker_renderer_identity"
                    ]
                )
            },
            "pins": copy.deepcopy(identity["runtime_and_implementation_pins"]),
        }
        performance = {
            "schema_version": 1,
            "kind": "cx_local_cycles_spec_benchmark",
            "evidence": "measured",
            "cache_used": False,
            "frame": 9,
            "reference_samples": cache.REFERENCE_SAMPLES,
            "resolution": list(cache.DIMENSIONS),
            "device": "GPU/METAL",
            "scene": "/frozen/main.blend",
            "scene_sha256": "3" * 64,
            "baseline_s": 112.0,
            "preview_only": True,
            "production_ready": False,
            "receipt_trust": "local_unattested",
            "worker_renderer_identity": copy.deepcopy(
                identity["render_recipe_and_policy"]["worker_renderer_identity"]
            ),
            "pins": copy.deepcopy(identity["runtime_and_implementation_pins"]),
            "benchmark_audit": {
                "kind": "cx_cycles_preview_benchmark_audit",
                "schema_version": 2,
                "measurement_only": True,
                "binding_sha256": identity["manifest_binding_sha256"],
                "baseline": {
                    "artifact_sha256": "c" * 64,
                    "manifest_path": "unit/baseline-manifest.json",
                    "phase": "baseline",
                    "sample_offset": identity["render_recipe_and_policy"].get(
                        "sample_offset"
                    ),
                },
            },
        }
        valid = {
            "performance": performance,
            "source": source,
            "source_manifest": source_manifest,
            "exact_baseline": 112.0,
        }

        def changed_value(value: object) -> object:
            if type(value) is bool:
                return not value
            if type(value) is int:
                return value + 1
            if isinstance(value, float):
                return value + 1.0
            if isinstance(value, list):
                return [value[0] + 1, *value[1:]]
            if isinstance(value, str) and len(value) == 64:
                return ("9" if value[0] != "9" else "8") * 64
            if isinstance(value, str):
                return f"{value}.forged"
            if value is None:
                return 1
            self.fail(f"no forgery transform for {value!r}")

        def mutate(context: dict[str, object], path: tuple[str, ...]) -> None:
            current: object = context
            for key in path[:-1]:
                current = current[key]  # type: ignore[index]
            key = path[-1]
            current[key] = changed_value(current[key])  # type: ignore[index]

        cache._validate_source_performance_binding(**copy.deepcopy(valid))
        paths = [
            ("performance", key)
            for key in (
                "schema_version",
                "kind",
                "evidence",
                "cache_used",
                "frame",
                "reference_samples",
                "resolution",
                "device",
                "scene",
                "scene_sha256",
                "baseline_s",
                "preview_only",
                "production_ready",
                "receipt_trust",
            )
        ]
        paths.extend(
            (
                "performance",
                "benchmark_audit",
                "baseline",
                key,
            )
            for key in (
                "artifact_sha256",
                "manifest_path",
                "phase",
                "sample_offset",
            )
        )
        paths.extend(
            ("performance", "benchmark_audit", key)
            for key in ("kind", "schema_version", "measurement_only", "binding_sha256")
        )
        paths.extend(
            ("performance", "pins", key)
            for key in (
                "backend_sha256",
                "blender_sha256",
                "controller_adapter_sha256",
                "controller_core_sha256",
            )
        )
        paths.extend(
            [
                (
                    "performance",
                    "worker_renderer_identity",
                    "version",
                ),
                ("source", "artifact", "sha256"),
                (
                    "source",
                    "serialized_scene_identity",
                    "main_blend_sha256",
                ),
                ("source_manifest", "artifact", "path"),
                ("source_manifest", "artifact", "sha256"),
                ("source_manifest", "scene", "sha256"),
                ("source_manifest", "binding_sha256"),
                (
                    "source_manifest",
                    "render",
                    "worker_renderer_identity",
                    "version",
                ),
                ("source_manifest", "pins", "backend_sha256"),
                ("exact_baseline",),
            ]
        )
        for path in paths:
            with self.subTest(component=".".join(path)):
                changed = copy.deepcopy(valid)
                mutate(changed, path)
                with self.assertRaises(cache.CacheScreenError):
                    cache._validate_source_performance_binding(**changed)

    def test_target_baseline_substitution_rejected_with_file_verification(self) -> None:
        reference = publish_fixture(self.root, "evidence/reference.png", b"reference")
        scene = {
            "bundle_bytes": 100,
            "bundle_files": 2,
            "bundle_sha256": "2" * 64,
            "relative_path": "main.blend",
            "sha256": "3" * 64,
        }
        manifest = {
            "schema_version": 2,
            "kind": cache.MANIFEST_KIND,
            "phase": "baseline",
            "artifact": {
                "media_type": "image/png",
                "sha256": reference["sha256"],
            },
            "render": {
                "frame": 10,
                "width": cache.DIMENSIONS[0],
                "height": cache.DIMENSIONS[1],
                "samples": cache.REFERENCE_SAMPLES,
                "worker_renderer_identity": {},
            },
            "scene": scene,
        }
        manifest_data = cache.canonical_json(manifest) + b"\n"
        manifest_record = publish_fixture(
            self.root, "evidence/manifest.json", manifest_data
        )
        projected_scene, projected_recipe = cache._manifest_projection(
            manifest,
            expected_frame=10,
            expected_sha256=reference["sha256"],
        )
        frontier = {
            "schema_version": 1,
            "kind": cache.FRONTIER_KIND,
            "frame": 10,
            "baseline_s": 112.0,
            "reference": {"samples": cache.REFERENCE_SAMPLES},
            "artifacts": {
                "baseline": {
                    "bytes": reference["bytes"],
                    "sha256": reference["sha256"],
                },
                "baseline_manifest": {
                    "bytes": manifest_record["bytes"],
                    "sha256": manifest_record["sha256"],
                },
            },
        }
        frontier_record = publish_fixture(
            self.root,
            "evidence/frontier.json",
            cache.canonical_json(frontier) + b"\n",
        )
        target = {
            "frame": 10,
            "baseline_s": 112.0,
            "reference": reference,
            "frontier_receipt": frontier_record,
            "baseline_manifest": manifest_record,
            "serialized_scene_identity": projected_scene,
            "render_recipe_excluding_frame_and_seed": projected_recipe,
        }
        cache._validate_target_evidence_files(
            target, frame=10, baseline_s=112.0, screen_root=self.root
        )
        changed = copy.deepcopy(target)
        changed["baseline_s"] = 1.0
        with self.assertRaisesRegex(cache.CacheScreenError, "receipt binding"):
            cache._validate_target_evidence_files(
                changed, frame=10, baseline_s=1.0, screen_root=self.root
            )

    def test_named_proof_swap_and_fabricated_summary_are_rejected(self) -> None:
        candidate = publish_fixture(self.root, "evidence/candidate.png", b"candidate")
        reference = publish_fixture(self.root, "evidence/reference.png", b"reference")
        proof, verification = quality_fixture(candidate, reference)
        proof_record = publish_fixture(
            self.root, "evidence/proof.json", cache.canonical_json(proof) + b"\n"
        )
        verification_record = publish_fixture(
            self.root,
            "evidence/verification.json",
            cache.canonical_json(verification) + b"\n",
        )
        audit = {
            "candidate": candidate,
            "reference": reference,
            "proof": proof_record,
            "verification": verification_record,
            "summary": cache._quality_summary(proof),
        }
        pins = cache.implementation_pins()
        with mock.patch.object(
            cache,
            "_recompute_independent_verification",
            return_value=verification,
        ):
            cache._validate_quality_audit_files(
                label="frame_9_vs_frame_10",
                audit=audit,
                candidate_record=candidate,
                reference_record=reference,
                screen_root=self.root,
                pins=pins,
            )
            fabricated = copy.deepcopy(audit)
            fabricated["summary"]["alpha_agreement"] = 0.0
            with self.assertRaisesRegex(cache.CacheScreenError, "evidence changed"):
                cache._validate_quality_audit_files(
                    label="frame_9_vs_frame_10",
                    audit=fabricated,
                    candidate_record=candidate,
                    reference_record=reference,
                    screen_root=self.root,
                    pins=pins,
                )

        other_reference = publish_fixture(
            self.root, "evidence/other-reference.png", b"other-reference"
        )
        other_proof, other_verification = quality_fixture(candidate, other_reference)
        swapped = copy.deepcopy(audit)
        swapped["proof"] = publish_fixture(
            self.root,
            "evidence/swapped-proof.json",
            cache.canonical_json(other_proof) + b"\n",
        )
        swapped["verification"] = publish_fixture(
            self.root,
            "evidence/swapped-verification.json",
            cache.canonical_json(other_verification) + b"\n",
        )
        with self.assertRaisesRegex(cache.CacheScreenError, "did not close"):
            cache._validate_quality_audit_files(
                label="frame_9_vs_frame_10",
                audit=swapped,
                candidate_record=candidate,
                reference_record=reference,
                screen_root=self.root,
                pins=pins,
            )

    def test_fabricated_one_nanosecond_aggregate_total_is_rejected(self) -> None:
        receipt = synthetic_receipt()
        cache.validate_screen_receipt(receipt, self.root, verify_files=False)
        changed = copy.deepcopy(receipt)
        changed["measurement"]["trials"][0]["timings_ns"]["total"] = 1
        changed["measurement"]["trials"][0]["total_s"] = 1e-9
        changed["measurement"]["samples_s"][0] = 1e-9
        with self.assertRaises(cache.CacheScreenError):
            cache.validate_screen_receipt(changed, self.root, verify_files=False)


if __name__ == "__main__":
    unittest.main()
