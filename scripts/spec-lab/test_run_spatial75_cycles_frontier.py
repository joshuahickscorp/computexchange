#!/usr/bin/env python3
"""Pure/adversarial tests for the spatial75 frontier measurement harness."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_spatial75_cycles_frontier as frontier  # noqa: E402
import cx_render_spatial75_v1 as spatial  # noqa: E402


SHA = "a" * 64


def record(path: str, size: int = 1, digest: str = SHA) -> dict[str, object]:
    return {"path": path, "bytes": size, "sha256": digest}


def spatial_timing_fixture(
    timing: float,
) -> tuple[dict[str, float], dict[str, object], dict[str, object]]:
    component = {
        "draft_endpoint_and_manifest": timing * 0.3,
        "verify_endpoint_and_manifest": timing * 0.3,
        "post_verify_wait_gate_and_publish": timing * 0.2,
    }
    gate_ns = round(timing * 0.05 * 1_000_000_000)
    publish_ns = round(timing * 0.02 * 1_000_000_000)
    authorization_ns = round(timing * 0.01 * 1_000_000_000)
    prepared_ns = round(timing * 0.03 * 1_000_000_000)
    postprocess_ns = round(timing * 0.1 * 1_000_000_000)
    pipeline = {
        "post_verify_snapshot_gate_publish_wall_s": component[
            "post_verify_wait_gate_and_publish"
        ],
        "verify_snapshot_conversion_s": timing * 0.01,
        "draft_snapshot_conversion_s": timing * 0.01,
        "draft_snapshot_convert_and_prepare_s": timing * 0.05,
        "prepare_submit_to_ready_s": timing * 0.06,
        "prepare_verify_overlap_s": timing * 0.05,
        "prepare_wait_tail_s": timing * 0.01,
        "verify_endpoint_wall_s": timing * 0.31,
        "fused_gate_publish_tail_s": timing * 0.1,
        "gate_tail_s": gate_ns / 1_000_000_000,
        "publish_tail_s": publish_ns / 1_000_000_000,
        "fused_publish_tail_s": publish_ns / 1_000_000_000,
        "fused_authorization_tail_s": authorization_ns / 1_000_000_000,
        "prepared": {"timings_ns": {"total": prepared_ns}},
    }
    spatial = {
        "timings_ns": {
            "gate": gate_ns,
            "publish": publish_ns,
            "selected_draft_postprocess": postprocess_ns,
        },
        "gate": {
            "timings_ns": {
                "total": gate_ns,
                "authorization": authorization_ns,
            }
        },
        "postprocess": {
            "timings_ns": {
                "publish": publish_ns,
                "total": postprocess_ns,
                "prepared_total": prepared_ns,
            }
        },
    }
    return component, pipeline, spatial


def synthetic_receipt() -> dict[str, object]:
    samples = [0.9, 1.0, 1.1]
    median = 1.0
    variance = sum((value - median) ** 2 for value in samples) / len(samples)
    p95 = frontier._type7_quantile(samples, 0.95)
    trial_artifacts = []
    trial_receipts = []
    for index, timing in enumerate(samples):
        component, pipeline, spatial = spatial_timing_fixture(timing)
        trial_artifacts.append(
            {
                "trial_index": index,
                "draft": record(f"trial-{index}/draft.png"),
                "draft_config": record(f"trial-{index}/draft-config.json"),
                "draft_manifest": record(f"trial-{index}/draft-manifest.json"),
                "verify": record(f"trial-{index}/verify.png"),
                "verify_config": record(f"trial-{index}/verify-config.json"),
                "verify_manifest": record(f"trial-{index}/verify-manifest.json"),
                "delivery": record(f"trial-{index}/delivery.png"),
            }
        )
        trial_receipts.append(
            {
                "trial_index": index,
                "predeclared_for_quality": index == 0,
                "spec_s": timing,
                "component_s": component,
                "pipeline_overlap": pipeline,
                "spatial75": spatial,
            }
        )
    quality_result = {
        "pass": True,
        "inputs": {
            "target_dimensions": list(frontier.DELIVERY_SIZE),
            "candidate": {"bytes": 1, "sha256": SHA, "mode": "RGBA"},
            "reference": {"bytes": 1, "sha256": SHA, "mode": "RGBA"},
        },
    }
    proof_sha = hashlib.sha256(frontier.canonical_json(quality_result)).hexdigest()
    verification = {
        "pass": True,
        "proof_verified": True,
        "quality_pass": True,
        "errors": [],
        "proof_result_sha256": proof_sha,
        "recomputed_result_sha256": proof_sha,
        "artifacts": {
            "candidate": {"bytes": 1, "sha256": SHA},
            "reference": {"bytes": 1, "sha256": SHA},
        },
    }
    command_count = 10
    return {
        "schema_version": frontier.SCHEMA_VERSION,
        "kind": frontier.KIND,
        "evidence": "synthetic",
        "receipt_trust": frontier.RECEIPT_TRUST,
        "claim_scope": "fixture",
        "timing_scope": "fixture",
        "preview_only": True,
        "trial_count": 3,
        "variance_estimate": variance,
        "timing_statistics": {
            "headline": "median_candidate_wall_seconds",
            "candidate_spec_s_samples": samples,
            "median_s": median,
            "p95_s_type7": p95,
            "minimum_s": min(samples),
            "maximum_s": max(samples),
            "population_variance_s2": variance,
            "reference_trial_count": 1,
        },
        "execution_order": ["fixture"],
        "execution_trace": {
            "worker_reused": True,
            "worker_command_count": command_count,
            "same_backend_session": True,
            "same_scene_bundle": True,
            "candidate_reference_seeds_distinct": True,
            "candidate_reference_sample_ranges_disjoint": True,
            "retained_validated_png_cache_empty": True,
            "candidate_handoff": "one_shot_backend_validated_png_snapshots",
            "commands": [
                {"command_id": index} for index in range(1, command_count + 1)
            ],
        },
        "reference_used_for_product_decision": False,
        "measurement_only": {},
        "device": "UNTRUSTED/CPU",
        "resident_policy": frontier.RESIDENT_POLICY,
        "scene": {"path": "/fixture.blend", "sha256": SHA},
        "frame": 1,
        "warmup": {},
        "candidate": {
            "resolution": list(frontier.LOW_SIZE),
            "draft_samples": frontier.DRAFT_SAMPLES,
            "verify_samples": frontier.VERIFY_SAMPLES,
            "draft_seed": 1,
            "verify_seed": 2,
            "sample_ranges": {
                "draft": [0, frontier.DRAFT_SAMPLES],
                "verify": [
                    frontier.DRAFT_SAMPLES,
                    frontier.DRAFT_SAMPLES + frontier.VERIFY_SAMPLES,
                ],
            },
            "sample_ranges_disjoint": True,
            "artifacts_distinct": True,
            "resident_mutations": [],
            "quality_trial_selection": {
                "predeclared_before_execution": True,
                "trial_index": 0,
                "selection_rule": "fixed_index_zero_not_timing_or_reference_selected",
            },
            "trials": trial_receipts,
            "spatial75": {},
        },
        "reference": {
            "resolution": list(frontier.DELIVERY_SIZE),
            "samples": frontier.REFERENCE_SAMPLES,
            "seed": 3,
            "sample_range": [
                frontier.DRAFT_SAMPLES + frontier.VERIFY_SAMPLES,
                frontier.DRAFT_SAMPLES
                + frontier.VERIFY_SAMPLES
                + frontier.REFERENCE_SAMPLES,
            ],
            "resident_mutation": "broad_v1",
            "single_trial_disclosed": True,
        },
        "baseline_s": 10.0,
        "spec_s": median,
        "speedup_x": 10.0,
        "quality_pass": True,
        "meets_200x_verified": False,
        "meets_1000x_verified": False,
        "quality_v3": {
            "measurement_only": True,
            "result": quality_result,
            "independent_verification": verification,
            "artifact": record("quality.json"),
            "verification_artifact": record("verification.json"),
        },
        "artifacts": {
            "warmup": {
                "draft": record("warm/draft.png"),
                "draft_config": record("warm/draft-config.json"),
                "verify": record("warm/verify.png"),
                "verify_config": record("warm/verify-config.json"),
                "delivery": record("warm/delivery.png"),
            },
            "candidate_trials": trial_artifacts,
            "predeclared_quality_trial_index": 0,
            "baseline": record("baseline.png"),
            "baseline_config": record("baseline-config.json"),
            "baseline_manifest": record("baseline-manifest.json"),
            "rearm": record("rearm.png"),
            "rearm_config": record("rearm-config.json"),
            "timing_evidence": record("frontier-timing-evidence.json"),
        },
        "renderer_identity": {"executable_sha256": SHA},
        "host": {},
        "pins": frontier._code_pins(),
    }


def write_record(root: Path, target: dict[str, object], data: bytes) -> None:
    path = root / str(target["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    target.update(
        bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def json_bytes(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        indent=2,
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii") + b"\n"


def publish_synthetic_receipt(root: Path) -> dict[str, object]:
    receipt = synthetic_receipt()
    artifacts = receipt["artifacts"]
    records: list[dict[str, object]] = []
    records.extend(artifacts["warmup"].values())
    for trial in artifacts["candidate_trials"]:
        records.extend(
            value for key, value in trial.items() if key != "trial_index"
        )
    records.extend(
        artifacts[key]
        for key in (
            "baseline",
            "baseline_config",
            "baseline_manifest",
            "rearm",
            "rearm_config",
        )
    )
    for index, artifact_record in enumerate(records):
        write_record(
            root,
            artifact_record,
            f"immutable-artifact-{index}-{artifact_record['path']}".encode(),
        )

    selected = artifacts["candidate_trials"][0]["delivery"]
    baseline = artifacts["baseline"]
    result = receipt["quality_v3"]["result"]
    result["inputs"]["candidate"].update(
        bytes=selected["bytes"], sha256=selected["sha256"]
    )
    result["inputs"]["reference"].update(
        bytes=baseline["bytes"], sha256=baseline["sha256"]
    )
    proof_sha = hashlib.sha256(frontier.canonical_json(result)).hexdigest()
    verification = receipt["quality_v3"]["independent_verification"]
    verification.update(
        proof_result_sha256=proof_sha,
        recomputed_result_sha256=proof_sha,
    )
    verification["artifacts"] = {
        "candidate": {"bytes": selected["bytes"], "sha256": selected["sha256"]},
        "reference": {"bytes": baseline["bytes"], "sha256": baseline["sha256"]},
    }
    write_record(
        root,
        receipt["quality_v3"]["artifact"],
        json_bytes(result),
    )
    write_record(
        root,
        receipt["quality_v3"]["verification_artifact"],
        json_bytes(verification),
    )
    timing = frontier._timing_evidence(
        baseline_s=receipt["baseline_s"],
        trials=receipt["candidate"]["trials"],
        pins=receipt["pins"],
    )
    write_record(
        root,
        artifacts["timing_evidence"],
        json_bytes(timing),
    )
    return receipt


class FrontierHarnessTest(unittest.TestCase):
    def test_type7_timing_quantile_and_argument_safety(self) -> None:
        self.assertAlmostEqual(frontier._type7_quantile([1, 2, 3], 0.95), 2.9)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            scene = root / "scene.blend"
            scene.write_bytes(b"blend")
            blender = root / "blender"
            blender.write_text("#!/bin/sh\nexit 0\n")
            blender.chmod(0o700)
            output = root / "output"
            args = frontier.parse_args(
                [
                    "--scene",
                    str(scene),
                    "--blender",
                    str(blender),
                    "--frame",
                    "1",
                    "--output-root",
                    str(output),
                ]
            )
            frontier.validate_args(args)
            args.candidate_trials = 4
            with self.assertRaisesRegex(frontier.FrontierError, "odd"):
                frontier.validate_args(args)
            args.candidate_trials = 3
            args.json_out = output
            with self.assertRaisesRegex(frontier.FrontierError, "alias"):
                frontier.validate_args(args)
            args.json_out = output / "quality-v3.json"
            with self.assertRaisesRegex(frontier.FrontierError, "receipt.json"):
                frontier.validate_args(args)

    def test_relative_record_rejects_symlink_and_json_is_no_clobber(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "target.bin"
            target.write_bytes(b"owned")
            link = root / "link.bin"
            try:
                link.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            with self.assertRaisesRegex(frontier.FrontierError, "non-symlink"):
                frontier._relative_record(link, root)
            output = root / "receipt.json"
            frontier._write_new_json(output, {"ok": True})
            with self.assertRaises(frontier.FrontierError):
                frontier._write_new_json(output, {"ok": False})
            self.assertEqual(json.loads(output.read_text()), {"ok": True})

    def test_json_publication_rejects_destination_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            output = root / "receipt.json"
            real_fsync = os.fsync
            calls = 0

            def substitute_on_directory_fsync(descriptor: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    output.unlink()
                    output.write_bytes(b"replacement")
                real_fsync(descriptor)

            with mock.patch.object(frontier.os, "fsync", substitute_on_directory_fsync):
                with self.assertRaisesRegex(
                    frontier.FrontierError, "publication identity"
                ):
                    frontier._write_new_json(output, {"ok": True})
            self.assertEqual(output.read_bytes(), b"replacement")

    def test_spatial_artifact_binding_replays_gate_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            def materialize(name: str, data: bytes) -> dict[str, object]:
                target = root / name
                target.write_bytes(data)
                return {
                    "path": name,
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }

            artifacts = {
                "draft": materialize("draft.png", b"draft"),
                "verify": materialize("verify.png", b"verify"),
                "delivery": materialize("delivery.png", b"delivery"),
            }
            runtime = spatial.runtime_identity()
            policy_sha = hashlib.sha256(
                frontier.canonical_json(spatial.POLICY_DESCRIPTOR)
            ).hexdigest()
            runtime_sha = hashlib.sha256(
                frontier.canonical_json(runtime)
            ).hexdigest()
            gate = {
                "binding_sha256": SHA,
                "draft": {
                    "bytes": artifacts["draft"]["bytes"],
                    "decoded_identity_sha256": SHA,
                    "mode": "RGBA",
                    "sha256": artifacts["draft"]["sha256"],
                },
                "global_agreement": 1.0,
                "metric": "one_minus_mean_absolute_rgb_difference",
                "microtile_count": 1,
                "passed": True,
                "policy_id": spatial.POLICY_ID,
                "policy_sha256": policy_sha,
                "runtime": runtime,
                "runtime_sha256": runtime_sha,
                "thresholds": {
                    "global_minimum": spatial.GLOBAL_AGREEMENT_MIN,
                    "regional_worst_minimum": spatial.WORST_TILE_AGREEMENT_MIN,
                    "microtile_worst_minimum": spatial.MICROTILE_AGREEMENT_MIN,
                },
                "tiles": [{"rect": [0, 0, 1, 1], "score": 1.0}],
                "timings_ns": {"total": 1},
                "verify": {
                    "bytes": artifacts["verify"]["bytes"],
                    "decoded_identity_sha256": SHA,
                    "mode": "RGBA",
                    "sha256": artifacts["verify"]["sha256"],
                },
                "worst_microtile_agreement": 1.0,
                "worst_tile_agreement": 1.0,
            }
            receipt = {
                "gate": gate,
                "kind": spatial.RESULT_KIND,
                "policy": spatial.POLICY_DESCRIPTOR,
                "policy_sha256": policy_sha,
                "postprocess": {
                    "encoding": {"compression_level": 0, "format": "PNG", "optimize": False},
                    "experimental": True,
                    "input": {"sha256": artifacts["draft"]["sha256"]},
                    "input_decode_reused": True,
                    "operators": spatial.POLICY_DESCRIPTOR["transform"],
                    "output": {
                        "bytes": artifacts["delivery"]["bytes"],
                        "dimensions": list(frontier.DELIVERY_SIZE),
                        "mode": "RGBA",
                        "path": str((root / "delivery.png").resolve()),
                        "sha256": artifacts["delivery"]["sha256"],
                    },
                    "policy_id": spatial.POLICY_ID,
                    "policy_sha256": policy_sha,
                    "quality_claim": False,
                    "runtime": runtime,
                    "timings_ns": {},
                },
                "runtime": runtime,
                "schema_version": spatial.SCHEMA_VERSION,
                "timings_ns": {},
            }
            replayed = SimpleNamespace(receipt=lambda: gate)
            prepared = SimpleNamespace(
                input_sha256=artifacts["draft"]["sha256"],
                input_bytes=artifacts["draft"]["bytes"],
                output_sha256=artifacts["delivery"]["sha256"],
                output_bytes=artifacts["delivery"]["bytes"],
            )
            pins = frontier._code_pins()
            with (
                mock.patch.object(spatial, "gate_pngs", return_value=replayed),
                mock.patch.object(spatial, "decode_png", return_value=(object(), {})),
                mock.patch.object(
                    spatial, "prepare_decoded_draft", return_value=prepared
                ),
            ):
                frontier._validate_spatial_artifact_binding(
                    receipt, artifacts, output_root=root, pins=pins
                )
                forged = json.loads(json.dumps(receipt))
                forged["gate"]["passed"] = False
                with self.assertRaisesRegex(frontier.FrontierError, "artifact binding"):
                    frontier._validate_spatial_artifact_binding(
                        forged, artifacts, output_root=root, pins=pins
                    )
                forged = json.loads(json.dumps(receipt))
                forged["gate"]["binding_sha256"] = "0" * 64
                with self.assertRaisesRegex(frontier.FrontierError, "gate replay"):
                    frontier._validate_spatial_artifact_binding(
                        forged, artifacts, output_root=root, pins=pins
                    )
                forged = json.loads(json.dumps(receipt))
                forged["postprocess"]["operators"] = {}
                with self.assertRaisesRegex(frontier.FrontierError, "artifact binding"):
                    frontier._validate_spatial_artifact_binding(
                        forged, artifacts, output_root=root, pins=pins
                    )
                forged = json.loads(json.dumps(receipt))
                forged["gate"]["draft"]["sha256"] = "0" * 64
                with self.assertRaisesRegex(frontier.FrontierError, "artifact binding"):
                    frontier._validate_spatial_artifact_binding(
                        forged, artifacts, output_root=root, pins=pins
                    )

    def test_render_config_attestation_rejects_seed_policy_and_extra_fields(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            output = root / "draft.png"
            config = {
                "border": None,
                "candidate_denoising_policy": dict(frontier.DENOISING_OFF_POLICY),
                "candidate_profile": dict(frontier.NATIVE_CANDIDATE_PROFILE),
                "frame": 9,
                "height": frontier.LOW_SIZE[1],
                "output": str(output),
                "phase": "draft",
                "resident_policy": frontier.RESIDENT_POLICY,
                "sample_offset": 0,
                "samples": 4,
                "seed": 17,
                "width": frontier.LOW_SIZE[0],
            }
            path = root / "config.json"
            path.write_text(json.dumps(config))
            frontier._validate_render_config(
                path,
                phase="draft",
                frame=9,
                size=frontier.LOW_SIZE,
                samples=4,
                sample_offset=0,
                seed=17,
                output=output,
            )
            for label, mutate in (
                ("seed", lambda value: value.update(seed=18)),
                ("policy", lambda value: value.update(resident_policy="broad_v1")),
                ("extra", lambda value: value.update(extra=True)),
            ):
                with self.subTest(label=label):
                    changed = dict(config)
                    mutate(changed)
                    path.write_text(json.dumps(changed))
                    with self.assertRaises(frontier.FrontierError):
                        frontier._validate_render_config(
                            path,
                            phase="draft",
                            frame=9,
                            size=frontier.LOW_SIZE,
                            samples=4,
                            sample_offset=0,
                            seed=17,
                            output=output,
                        )
            changed = json.loads(json.dumps(config))
            changed["candidate_profile"]["extra"] = True
            path.write_text(json.dumps(changed))
            with self.assertRaises(frontier.FrontierError):
                frontier._validate_render_config(
                    path,
                    phase="draft",
                    frame=9,
                    size=frontier.LOW_SIZE,
                    samples=4,
                    sample_offset=0,
                    seed=17,
                    output=output,
                )

    def test_render_manifest_attests_mutation_and_restored_reference_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "draft.png"
            artifact.write_bytes(b"png")
            artifact_sha = hashlib.sha256(b"png").hexdigest()
            native = {
                "max_bounces": 32,
                "diffuse_bounces": 16,
                "glossy_bounces": 16,
                "transmission_bounces": 32,
            }
            sampling = {
                "use_light_tree": False,
                "use_adaptive_sampling": False,
                "adaptive_min_samples": 0,
                "adaptive_threshold": 0.01,
            }
            denoising = {
                "use_denoising": False,
                "view_layer_use_denoising": False,
                "denoiser": None,
                "denoising_input_passes": None,
                "denoising_prefilter": None,
                "denoising_quality": None,
                "denoising_use_gpu": None,
            }
            worker = {
                "resident_policy": frontier.RESIDENT_POLICY,
                "candidate_profile": {"name": frontier.CANDIDATE_PROFILE},
                "native_integrator": native,
                "reference_sampling": sampling,
                "reference_denoising_policy": denoising,
            }
            manifest = {
                "kind": "cx_cycles_preview_manifest",
                "phase": "draft",
                "render": {
                    "frame": 9,
                    "width": frontier.LOW_SIZE[0],
                    "height": frontier.LOW_SIZE[1],
                    "samples": 4,
                    "sample_offset": 0,
                    "seed": 17,
                    "resident_policy": frontier.RESIDENT_POLICY,
                    "resident_mutation": frontier.RESIDENT_POLICY,
                    "worker_renderer_identity": worker,
                    "integrator_policy": {
                        "mode": "fixed_reference",
                        "actual_integrator": native,
                        "actual_sampling": sampling,
                    },
                    "denoising_policy": {
                        "mode": "fixed_off_reference",
                        "actual": denoising,
                    },
                },
                "artifact": {"path": "draft.png", "sha256": artifact_sha},
            }
            path = root / "manifest.json"
            path.write_text(json.dumps(manifest))
            kwargs = {
                "phase": "draft",
                "frame": 9,
                "size": frontier.LOW_SIZE,
                "samples": 4,
                "sample_offset": 0,
                "seed": 17,
                "mutation": frontier.RESIDENT_POLICY,
                "artifact": artifact,
                "artifact_sha256": artifact_sha,
                "output_root": root,
            }
            frontier._validate_render_manifest(path, **kwargs)
            manifest["render"]["resident_mutation"] = "broad_v1"
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(frontier.FrontierError, "identity"):
                frontier._validate_render_manifest(path, **kwargs)
            manifest["render"]["resident_mutation"] = frontier.RESIDENT_POLICY
            manifest["render"]["integrator_policy"]["actual_integrator"] = {
                **native,
                "max_bounces": 31,
            }
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(frontier.FrontierError, "identity"):
                frontier._validate_render_manifest(path, **kwargs)

    def test_same_worker_trace_rejects_restart_seed_collision_and_bad_auth(self) -> None:
        class Process:
            def poll(self):
                return None

        class Backend:
            RESIDENT_POLICY_BROAD = "broad_v1"

            def _worker_key(self, _context):
                return ("same-worker",)

        backend = Backend()
        session = {
            "candidate_profile_auth": "c" * 64,
            "candidate_profile_scope": frontier.CANDIDATE_PROFILE_BENCHMARK_SCOPE,
            "resident_policy": frontier.RESIDENT_POLICY,
            "candidate_profile": {"name": frontier.CANDIDATE_PROFILE},
        }
        bundle = {"sha256": SHA}
        low = {
            "session": session,
            "scene_copy": Path("scene.blend"),
            "scene_bundle": bundle,
            "seeds": {"draft": 1, "verify": 2},
            "resident_mutation_history": [
                {"command_id": 1, "frame": 9, "phase": "draft", "mutation": "broad_v1"},
                {"command_id": 2, "frame": 9, "phase": "verify", "mutation": frontier.RESIDENT_POLICY},
                *[
                    {
                        "command_id": command_id,
                        "frame": 9,
                        "phase": "draft" if command_id % 2 else "verify",
                        "mutation": frontier.RESIDENT_POLICY,
                    }
                    for command_id in range(3, 9)
                ],
            ],
        }
        rearm = {
            "session": session,
            "scene_copy": low["scene_copy"],
            "scene_bundle": bundle,
            "resident_mutation_history": [
                {"command_id": 9, "frame": 8, "phase": "draft", "mutation": "broad_v1"}
            ],
        }
        full = {
            "session": session,
            "scene_copy": low["scene_copy"],
            "scene_bundle": bundle,
            "seeds": {"baseline": 3},
            "resident_mutation_history": [
                {"command_id": 10, "frame": 9, "phase": "baseline", "mutation": "broad_v1"}
            ],
        }
        backend._WORKER = {
            "commands": 10,
            "process": Process(),
            "key": ("same-worker",),
        }
        result = frontier._validate_execution_trace(
            backend,
            low,
            rearm,
            full,
            frame=9,
            rearm_frame=8,
            capability="c" * 64,
            candidate_trials=3,
        )
        self.assertTrue(result["worker_reused"])
        backend._WORKER["commands"] = 1
        with self.assertRaisesRegex(frontier.FrontierError, "not reused"):
            frontier._validate_execution_trace(
                backend,
                low,
                rearm,
                full,
                frame=9,
                rearm_frame=8,
                capability="c" * 64,
                candidate_trials=3,
            )
        backend._WORKER["commands"] = 10
        full["seeds"]["baseline"] = 2
        with self.assertRaisesRegex(frontier.FrontierError, "seeds"):
            frontier._validate_execution_trace(
                backend,
                low,
                rearm,
                full,
                frame=9,
                rearm_frame=8,
                capability="c" * 64,
                candidate_trials=3,
            )
        full["seeds"]["baseline"] = 3
        session["candidate_profile_auth"] = "d" * 64
        with self.assertRaisesRegex(frontier.FrontierError, "capability"):
            frontier._validate_execution_trace(
                backend,
                low,
                rearm,
                full,
                frame=9,
                rearm_frame=8,
                capability="c" * 64,
                candidate_trials=3,
            )

    def test_pipeline_publishes_only_after_passed_bound_gate(self) -> None:
        output_root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(output_root))
        delivery = output_root / "delivery.png"
        prepared = SimpleNamespace(
            receipt=lambda: {"binding": SHA},
            timings_ns=SimpleNamespace(total=10),
        )
        decoded = object()
        gate = SimpleNamespace(
            passed=True,
            timings_ns={
                "read_verify": 1,
                "decode_verify": 1,
                "difference": 1,
                "partition": 1,
                "score": 1,
                "total": 5,
            },
        )
        post = SimpleNamespace(
            timings_ns={
                "transform": 1,
                "encode": 1,
                "validate": 1,
                "publish": 1,
                "total": 12,
            }
        )

        class BenchmarkResult:
            def __init__(self, *, gate, postprocess, timings_ns):
                self.gate = gate
                self.postprocess = postprocess
                self.timings_ns = timings_ns

        class GateRejected(Exception):
            pass

        def fused_publish(_prepared, _draft, _verify, path):
            Path(path).write_bytes(b"png")
            return SimpleNamespace(
                gate=gate,
                postprocess=post,
                timings_ns={
                    "authorization": 2,
                    "difference": 1,
                    "partition": 1,
                    "score": 1,
                    "publish": 3,
                    "total": 8,
                },
            )

        spatial = SimpleNamespace(
            gate_decoded_pair_and_publish_prepared=mock.Mock(
                side_effect=fused_publish
            ),
            GateRejected=GateRejected,
            BenchmarkResult=BenchmarkResult,
        )
        bundle = {
            "decoded": decoded,
            "snapshot": {
                "source_bytes": 3,
                "sha256": SHA,
                "mode": "RGB",
                "pixel_bytes": 12,
                "pixel_sha256": SHA,
            },
            "snapshot_conversion_s": 0.01,
            "prepared": prepared,
            "started": 1.0,
            "finished": 1.1,
        }
        result, telemetry = frontier._pipelined_spatial_result(
            spatial, bundle, output_root / "verify.png", delivery
        )
        self.assertTrue(delivery.is_file())
        self.assertIs(result.gate, gate)
        self.assertIn("gate_tail_s", telemetry)
        self.assertEqual(telemetry["fused_authorization_tail_s"], 2e-9)
        self.assertEqual(telemetry["fused_publish_tail_s"], 3e-9)
        self.assertEqual(result.timings_ns["total"], 18)
        spatial.gate_decoded_pair_and_publish_prepared.side_effect = GateRejected
        rejected = output_root / "rejected.png"
        with self.assertRaisesRegex(frontier.FrontierError, "rejected"):
            frontier._pipelined_spatial_result(
                spatial, bundle, output_root / "verify.png", rejected
            )
        self.assertFalse(rejected.exists())

    def test_receipt_validator_closes_statistics_quality_and_artifacts(self) -> None:
        receipt = synthetic_receipt()
        frontier.validate_receipt(receipt)
        mutations = (
            ("median", lambda value: value["timing_statistics"].update(median_s=0.8)),
            ("speed", lambda value: value.update(speedup_x=11.0)),
            (
                "selection",
                lambda value: value["candidate"]["quality_trial_selection"].update(
                    trial_index=1
                ),
            ),
            (
                "quality-binding",
                lambda value: value["quality_v3"]["result"]["inputs"][
                    "candidate"
                ].update(sha256="b" * 64),
            ),
            (
                "component-omission",
                lambda value: value["candidate"]["trials"][0][
                    "component_s"
                ].update(draft_endpoint_and_manifest=2.0),
            ),
        )
        import copy

        for label, mutate in mutations:
            with self.subTest(label=label):
                changed = copy.deepcopy(receipt)
                mutate(changed)
                with self.assertRaises(frontier.FrontierError):
                    frontier.validate_receipt(changed)

    def test_receipt_validator_requires_exact_current_code_pins(self) -> None:
        import copy

        receipt = synthetic_receipt()
        frontier.validate_receipt(receipt)
        changed = copy.deepcopy(receipt)
        first = next(iter(changed["pins"]))
        changed["pins"][first] = "b" * 64
        with self.assertRaisesRegex(frontier.FrontierError, "exact current pins"):
            frontier.validate_receipt(changed)

    def test_immutable_timing_evidence_rejects_fabricated_1000x(self) -> None:
        import copy

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            receipt = publish_synthetic_receipt(root)
            verification = receipt["quality_v3"]["independent_verification"]
            with mock.patch.object(
                frontier,
                "_recompute_quality_verification",
                return_value=verification,
            ):
                frontier.validate_receipt(receipt, output_root=root)

            forged = copy.deepcopy(receipt)
            samples = [0.001, 0.001, 0.001]
            forged["timing_statistics"].update(
                candidate_spec_s_samples=samples,
                median_s=0.001,
                p95_s_type7=0.001,
                minimum_s=0.001,
                maximum_s=0.001,
                population_variance_s2=0.0,
            )
            forged["variance_estimate"] = 0.0
            forged["evidence"] = "measured"
            forged["renderer_identity"]["official_signed_executable"] = True
            forged["spec_s"] = 0.001
            forged["speedup_x"] = forged["baseline_s"] / 0.001
            forged["meets_200x_verified"] = True
            forged["meets_1000x_verified"] = True
            for trial in forged["candidate"]["trials"]:
                component, pipeline, spatial = spatial_timing_fixture(0.001)
                trial.update(
                    spec_s=0.001,
                    component_s=component,
                    pipeline_overlap=pipeline,
                    spatial75=spatial,
                )
            with mock.patch.object(
                frontier,
                "_recompute_quality_verification",
                return_value=verification,
            ):
                with self.assertRaisesRegex(
                    frontier.FrontierError, "immutable timing evidence"
                ):
                    frontier.validate_receipt(forged, output_root=root)

    def test_quality_artifacts_and_current_verifier_defeat_forged_proof(self) -> None:
        import copy

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            receipt = publish_synthetic_receipt(root)
            original_verification = copy.deepcopy(
                receipt["quality_v3"]["independent_verification"]
            )
            with mock.patch.object(
                frontier,
                "_recompute_quality_verification",
                return_value=original_verification,
            ):
                frontier.validate_receipt(receipt, output_root=root)

            embedded_only = copy.deepcopy(receipt)
            embedded_only["quality_v3"]["result"]["forged_metric"] = 1.0
            forged_sha = hashlib.sha256(
                frontier.canonical_json(embedded_only["quality_v3"]["result"])
            ).hexdigest()
            embedded_only["quality_v3"]["independent_verification"].update(
                proof_result_sha256=forged_sha,
                recomputed_result_sha256=forged_sha,
            )
            with mock.patch.object(
                frontier,
                "_recompute_quality_verification",
                return_value=original_verification,
            ):
                with self.assertRaisesRegex(
                    frontier.FrontierError, "stored quality evidence"
                ):
                    frontier.validate_receipt(embedded_only, output_root=root)

            fully_forged = copy.deepcopy(receipt)
            fully_forged["quality_v3"]["result"]["forged_metric"] = 1.0
            forged_sha = hashlib.sha256(
                frontier.canonical_json(fully_forged["quality_v3"]["result"])
            ).hexdigest()
            fully_forged["quality_v3"]["independent_verification"].update(
                proof_result_sha256=forged_sha,
                recomputed_result_sha256=forged_sha,
            )
            write_record(
                root,
                fully_forged["quality_v3"]["artifact"],
                json_bytes(fully_forged["quality_v3"]["result"]),
            )
            write_record(
                root,
                fully_forged["quality_v3"]["verification_artifact"],
                json_bytes(
                    fully_forged["quality_v3"]["independent_verification"]
                ),
            )
            with mock.patch.object(
                frontier,
                "_recompute_quality_verification",
                return_value=original_verification,
            ):
                with self.assertRaisesRegex(
                    frontier.FrontierError, "independent quality-v3 replay"
                ):
                    frontier.validate_receipt(fully_forged, output_root=root)


if __name__ == "__main__":
    unittest.main()
