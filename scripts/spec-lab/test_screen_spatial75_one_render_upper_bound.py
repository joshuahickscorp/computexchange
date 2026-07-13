#!/usr/bin/env python3
"""Pure/adversarial tests for the spatial75 one-render upper-bound screen."""

from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import screen_spatial75_one_render_upper_bound as screen  # noqa: E402


def _record(root: Path, name: str, data: bytes) -> dict[str, object]:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {
        "path": name,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _json_record(root: Path, name: str, value: dict[str, object]) -> dict[str, object]:
    data = json.dumps(
        value,
        sort_keys=True,
        indent=2,
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii") + b"\n"
    return _record(root, name, data)


def _policy() -> dict[str, object]:
    return {
        "declared_product_sampling": {
            "draft": {"sample_offset": 0, "samples": 4},
            "independent_verify": {"sample_offset": 4, "samples": 4},
            "sample_ranges_disjoint": True,
        },
        "measurement_sampling": {
            "independent_verify_executed": False,
            "sample_offset": 0,
            "samples": 4,
            "target_render_invocations_per_trial": 1,
        },
        "output": {
            "alpha_resample": "BICUBIC",
            "compression_level": 0,
            "dimensions": [1080, 1920],
            "format": "PNG",
            "optimize": False,
            "rgb_resample": "BICUBIC",
        },
        "spatial_policy_sha256": screen.EXPECTED_SPATIAL_POLICY_SHA256,
    }


def fixture_open(root: Path) -> dict[str, object]:
    policy = _policy()
    trials = []
    for index in range(screen.TRIAL_COUNT):
        artifacts = {
            "delivery": _record(root, f"trial-{index}-delivery.png", b"d" + bytes([index])),
            "draft": _record(root, f"trial-{index}-draft.png", b"r" + bytes([index])),
            "draft_config": _record(root, f"trial-{index}-config.json", b"c" + bytes([index])),
            "draft_manifest": _record(root, f"trial-{index}-manifest.json", b"m" + bytes([index])),
        }
        trials.append(
            {
                "artifacts": artifacts,
                "components_s": {
                    "target_endpoint_and_manifest_s": 0.02,
                    "immutable_snapshot_conversion_s": 0.02,
                    "reconstruction_encode_validate_bind_s": 0.02,
                    "measurement_only_atomic_publication_s": 0.02,
                },
                "pipeline": {
                    "publication": {
                        "authorization": False,
                        "independent_gate_executed": False,
                    }
                },
                "predeclared_for_quality": True,
                "trial_index": index,
                "unattributed_python_s": 0.02 + index * 0.001,
                "wall_s": 0.1 + index * 0.001,
            }
        )
    reference_data = b"pinned-reference"
    reference_path = root / "reference.png"
    reference_path.write_bytes(reference_data)
    receipt_data = b'{"fixture":"reference-receipt"}\n'
    receipt_path = root / "reference-receipt.json"
    receipt_path.write_bytes(receipt_data)
    warmup_artifacts = {
        "delivery": _record(root, "warmup-delivery.png", b"warm-delivery"),
        "draft": _record(root, "warmup-draft.png", b"warm-draft"),
        "draft_manifest": _record(root, "warmup-manifest.json", b"warm-manifest"),
    }
    component_names = tuple(trials[0]["components_s"])
    report = {
        "authorization": {
            "independent_pair_gate_executed": False,
            "independent_verify_render_executed": False,
            "measurement_only": True,
            "production_change_authorized": False,
            "publication_authorized": False,
            "reason": "fixture deliberately omits the independent pair gate",
        },
        "evidence": "measured",
        "execution_trace": {
            "independent_verify_commands": 0,
            "target_render_commands": screen.TRIAL_COUNT,
            "warmup_render_commands": 1,
        },
        "finalization": {
            "closed": False,
            "quality_v3_run": False,
            "quality_verifier_run": False,
        },
        "frame": screen.FRAME,
        "kind": screen.KIND,
        "pins": {
            **screen._code_pins(),
            "blender_executable_sha256": screen.EXPECTED_BLENDER_SHA256,
        },
        "policy": policy,
        "policy_sha256": screen.sha256_bytes(screen.canonical_json(policy)),
        "reference": {
            "artifact": {
                "bytes": len(reference_data),
                "path": str(reference_path),
                "sha256": hashlib.sha256(reference_data).hexdigest(),
            },
            "baseline_s": screen.EXPECTED_REFERENCE_BASELINE_S,
            "receipt": {
                "bytes": len(receipt_data),
                "path": str(receipt_path),
                "sha256": hashlib.sha256(receipt_data).hexdigest(),
            },
        },
        "renderer_identity": {
            "executable_sha256": screen.EXPECTED_BLENDER_SHA256,
            "official_signed_executable": True,
        },
        "scene": {"path": "/tmp/main.blend", "sha256": screen.EXPECTED_SCENE_SHA256},
        "schema_version": screen.SCHEMA_VERSION,
        "timing": {
            "component_statistics": {
                name: screen.timing_statistics(
                    [trial["components_s"][name] for trial in trials]
                )
                for name in component_names
            },
            "quality_and_verification_charged": False,
            "trial_wall_statistics": screen.timing_statistics(
                [trial["wall_s"] for trial in trials]
            ),
            "warmup_charged": False,
        },
        "trial_count": screen.TRIAL_COUNT,
        "trials": trials,
        "warmup": {
            "artifacts": warmup_artifacts,
            "charged": False,
            "pipeline": {"publication": {"authorization": False}},
            "target_endpoint_and_manifest_s": 0.1,
            "target_render_invocations": 1,
            "wall_s": 0.2,
        },
    }
    return report


def fixture_closed(root: Path) -> dict[str, object]:
    report = fixture_open(root)
    reference_record = report["reference"]["artifact"]
    for trial in report["trials"]:
        index = trial["trial_index"]
        candidate = trial["artifacts"]["delivery"]
        result = {
            "kind": "fixture_quality_v3",
            "pass": True,
            "inputs": {
                "candidate": {
                    "bytes": candidate["bytes"],
                    "sha256": candidate["sha256"],
                },
                "reference": {
                    "bytes": reference_record["bytes"],
                    "sha256": reference_record["sha256"],
                },
                "target_dimensions": list(screen.OUTPUT_SIZE),
            },
        }
        result_sha = screen.sha256_bytes(screen.canonical_json(result))
        verification = {
            "artifacts": {
                "candidate": {
                    "bytes": candidate["bytes"],
                    "sha256": candidate["sha256"],
                },
                "reference": {
                    "bytes": reference_record["bytes"],
                    "sha256": reference_record["sha256"],
                },
            },
            "errors": [],
            "pass": True,
            "proof_result_sha256": result_sha,
            "proof_verified": True,
            "quality_pass": True,
            "recomputed_result_sha256": result_sha,
        }
        proof = _json_record(root, f"quality-{index}.json", result)
        verification_artifact = _json_record(
            root,
            f"verification-{index}.json",
            verification,
        )
        trial["quality"] = {
            "charged_to_candidate_wall": False,
            "result": result,
            "independent_verification": verification,
            "proof_artifact": proof,
            "quality_v3_s": 0.1,
            "verification_artifact": verification_artifact,
            "verification_s": 0.1,
        }
    report["finalization"] = {
        "closed": True,
        "quality_and_verification_total_s": 1.0,
        "quality_v3_run": True,
        "quality_verifier_run": True,
        "reference_used_for_candidate_selection": False,
    }
    report["conclusion"] = screen._expected_conclusion(report)
    report["budget_1000x"] = screen._expected_budget(report)
    return report


@contextmanager
def _validation_context(report: dict[str, object], *, replay: object | None = None):
    verification_by_proof = {
        str((Path(report["reference"]["artifact"]["path"]).parent / trial["quality"]["proof_artifact"]["path"]).resolve()): trial["quality"]["independent_verification"]
        for trial in report.get("trials", [])
        if "quality" in trial
    }

    def replay_stored(proof_path, _candidate_path, _reference_path):
        return copy.deepcopy(verification_by_proof[str(Path(proof_path).resolve())])

    replay_effect = replay_stored if replay is None else replay
    with mock.patch.object(
        screen,
        "_reference_binding",
        return_value=copy.deepcopy(report["reference"]),
    ), mock.patch.object(
        screen,
        "_recompute_quality_verification",
        side_effect=replay_effect,
    ):
        yield


class Spatial75OneRenderUpperBoundTest(unittest.TestCase):
    def test_code_pins_include_current_screen_and_adversarial_test(self) -> None:
        pins = screen._code_pins()
        self.assertEqual(
            pins["one_render_screen_sha256"],
            screen.sha256_file(HERE / "screen_spatial75_one_render_upper_bound.py"),
        )
        self.assertEqual(
            pins["one_render_screen_test_sha256"],
            screen.sha256_file(HERE / "test_screen_spatial75_one_render_upper_bound.py"),
        )

    def test_frozen_policy_is_exactly_one_measured_half_of_declared_four_plus_four(self) -> None:
        policy = _policy()
        self.assertEqual(policy["declared_product_sampling"]["draft"], {"sample_offset": 0, "samples": 4})
        self.assertEqual(policy["declared_product_sampling"]["independent_verify"], {"sample_offset": 4, "samples": 4})
        self.assertFalse(policy["measurement_sampling"]["independent_verify_executed"])
        self.assertEqual(policy["measurement_sampling"]["target_render_invocations_per_trial"], 1)
        self.assertEqual(policy["output"]["rgb_resample"], "BICUBIC")
        self.assertEqual(policy["output"]["alpha_resample"], "BICUBIC")
        self.assertEqual(policy["output"]["compression_level"], 0)

    def test_timing_statistics_use_seven_samples_and_type7_p95(self) -> None:
        result = screen.timing_statistics([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7])
        self.assertEqual(result["count"], 7)
        self.assertAlmostEqual(result["median_s"], 0.4)
        self.assertAlmostEqual(result["p95_s_type7"], 0.67)
        self.assertAlmostEqual(result["maximum_s"], 0.7)
        with self.assertRaisesRegex(screen.UpperBoundError, "seven trials"):
            screen.timing_statistics([0.1] * 5)

    def test_measurement_publication_validates_seal_then_publishes_without_gate(self) -> None:
        calls = []

        class FakeSpatial:
            @staticmethod
            def _validate_prepared_seal(prepared):
                calls.append(("seal", prepared))

            @staticmethod
            def _publish_new(path, data):
                calls.append(("publish", path, data))
                path.write_bytes(data)
                return 123

        prepared = SimpleNamespace(encoded_png=b"immutable")
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "delivery.png"
            result = screen._measurement_only_publish(
                FakeSpatial, prepared, destination
            )
            self.assertEqual(destination.read_bytes(), b"immutable")
        self.assertEqual([call[0] for call in calls], ["seal", "publish"])
        self.assertFalse(result["authorization"])
        self.assertFalse(result["independent_gate_executed"])

    def test_open_report_rejects_any_authorization_or_verify_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = fixture_open(root)
            with _validation_context(report):
                screen.validate_open_report(report, root)

            changed = copy.deepcopy(report)
            changed["authorization"]["publication_authorized"] = True
            with _validation_context(changed), self.assertRaisesRegex(
                screen.UpperBoundError,
                "claim broadened",
            ):
                screen.validate_open_report(changed, root)

            changed = copy.deepcopy(report)
            changed["execution_trace"]["independent_verify_commands"] = 1
            with _validation_context(changed), self.assertRaisesRegex(
                screen.UpperBoundError,
                "command trace",
            ):
                screen.validate_open_report(changed, root)

    def test_open_report_recomputes_component_and_wall_statistics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = fixture_open(root)

            changed = copy.deepcopy(report)
            changed["timing"]["trial_wall_statistics"]["median_s"] = 0.001
            with _validation_context(changed), self.assertRaisesRegex(
                screen.UpperBoundError,
                "wall statistics",
            ):
                screen.validate_open_report(changed, root)

            changed = copy.deepcopy(report)
            changed["trials"][2]["wall_s"] = 9.0
            with _validation_context(changed), self.assertRaisesRegex(
                screen.UpperBoundError,
                "does not close",
            ):
                screen.validate_open_report(changed, root)

    def test_closed_report_requires_all_independent_quality_proofs_but_stays_unauthorized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = fixture_closed(root)
            with _validation_context(report):
                screen.validate_closed_report(report, root)

            changed = copy.deepcopy(report)
            changed["trials"][3]["quality"]["independent_verification"][
                "proof_verified"
            ] = False
            with _validation_context(changed), self.assertRaisesRegex(
                screen.UpperBoundError,
                "quality verification",
            ):
                screen.validate_closed_report(changed, root)

            changed = copy.deepcopy(report)
            changed["conclusion"]["production_change_authorized"] = True
            with _validation_context(changed), self.assertRaisesRegex(
                screen.UpperBoundError,
                "conclusion",
            ):
                screen.validate_closed_report(changed, root)

    def test_closed_report_binds_embedded_proof_to_files_and_replays_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = fixture_closed(root)

            changed = copy.deepcopy(report)
            result = changed["trials"][0]["quality"]["result"]
            result["forged_note"] = "self-consistent embedded rewrite"
            result_sha = screen.sha256_bytes(screen.canonical_json(result))
            verification = changed["trials"][0]["quality"][
                "independent_verification"
            ]
            verification["proof_result_sha256"] = result_sha
            verification["recomputed_result_sha256"] = result_sha
            with _validation_context(changed), self.assertRaisesRegex(
                screen.UpperBoundError,
                "stored quality evidence",
            ):
                screen.validate_closed_report(changed, root)

            changed = copy.deepcopy(report)
            proof_path = root / changed["trials"][1]["quality"]["proof_artifact"]["path"]
            forged_proof = copy.deepcopy(changed["trials"][1]["quality"]["result"])
            forged_proof["forged_file"] = True
            changed["trials"][1]["quality"]["proof_artifact"] = _json_record(
                root,
                proof_path.name,
                forged_proof,
            )
            with _validation_context(changed), self.assertRaisesRegex(
                screen.UpperBoundError,
                "stored quality evidence",
            ):
                screen.validate_closed_report(changed, root)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = fixture_closed(root)

            def forged_replay(*_args):
                return {"pass": False, "proof_verified": False}

            with _validation_context(report, replay=forged_replay), self.assertRaisesRegex(
                screen.UpperBoundError,
                "independent quality replay",
            ):
                screen.validate_closed_report(report, root)

    def test_closed_report_derives_speedups_and_frame11_1000x_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = fixture_closed(root)
            self.assertEqual(
                report["budget_1000x"]["budget_s"],
                screen.EXPECTED_REFERENCE_BASELINE_S / 1000.0,
            )

            changed = copy.deepcopy(report)
            changed["conclusion"]["median_speedup_x"] = 10_000.0
            with _validation_context(changed), self.assertRaisesRegex(
                screen.UpperBoundError,
                "conclusion",
            ):
                screen.validate_closed_report(changed, root)

            changed = copy.deepcopy(report)
            changed["budget_1000x"]["source_baseline_s"] = 112.396726
            with _validation_context(changed), self.assertRaisesRegex(
                screen.UpperBoundError,
                "1000x budget",
            ):
                screen.validate_closed_report(changed, root)

    def test_artifact_resolution_rejects_symlink_ancestor_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
            root = Path(temporary)
            outside_root = Path(outside)
            data = b"outside-artifact"
            (outside_root / "artifact.bin").write_bytes(data)
            (root / "escape").symlink_to(outside_root, target_is_directory=True)
            record = {
                "path": "escape/artifact.bin",
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            with self.assertRaisesRegex(screen.UpperBoundError, "escaped its root"):
                screen._resolve_record(root, record, "escaped")

    def test_atomic_json_publication_is_no_clobber_and_descriptor_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "report.json"
            first = {"version": 1}
            second = {"version": 2}
            screen._write_json_atomic(path, first, replace=False)
            first_digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with self.assertRaisesRegex(screen.UpperBoundError, "refusing to replace"):
                screen._write_json_atomic(path, second, replace=False)
            screen._write_json_atomic(
                path,
                second,
                replace=True,
                expected_sha256=first_digest,
            )
            self.assertEqual(json.loads(path.read_bytes()), second)
            self.assertEqual([entry.name for entry in root.iterdir()], ["report.json"])

    def test_atomic_json_rejects_stage_and_destination_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "report.json"
            real_link = os.link

            def substitute_stage(src, dst, **kwargs):
                directory_fd = kwargs["src_dir_fd"]
                os.unlink(src, dir_fd=directory_fd)
                descriptor = os.open(
                    src,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=directory_fd,
                )
                try:
                    os.write(descriptor, b"attacker")
                finally:
                    os.close(descriptor)
                return real_link(src, dst, **kwargs)

            with mock.patch.object(screen.os, "link", side_effect=substitute_stage), self.assertRaisesRegex(
                screen.UpperBoundError,
                "participant was substituted",
            ):
                screen._write_json_atomic(path, {"safe": True}, replace=False)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "report.json"
            real_fsync = os.fsync
            attacked = False

            def substitute_destination(descriptor):
                nonlocal attacked
                if stat.S_ISDIR(os.fstat(descriptor).st_mode) and not attacked:
                    attacked = True
                    os.unlink(path.name, dir_fd=descriptor)
                    replacement = os.open(
                        path.name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=descriptor,
                    )
                    try:
                        os.write(replacement, b"attacker")
                    finally:
                        os.close(replacement)
                return real_fsync(descriptor)

            with mock.patch.object(screen.os, "fsync", side_effect=substitute_destination), self.assertRaisesRegex(
                screen.UpperBoundError,
                "changed during directory sync",
            ):
                screen._write_json_atomic(path, {"safe": True}, replace=False)

    def test_atomic_json_exchange_rejects_stage_or_prior_target_substitution(self) -> None:
        for replace_name in ("stage", "target"):
            with self.subTest(replace_name=replace_name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                path = root / "report.json"
                screen._write_json_atomic(path, {"version": 1}, replace=False)
                old_digest = hashlib.sha256(path.read_bytes()).hexdigest()
                real_exchange = screen._atomic_exchange

                def substitute(directory_fd, left, right):
                    victim = left if replace_name == "stage" else right
                    os.unlink(victim, dir_fd=directory_fd)
                    descriptor = os.open(
                        victim,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=directory_fd,
                    )
                    try:
                        os.write(descriptor, b"attacker")
                    finally:
                        os.close(descriptor)
                    real_exchange(directory_fd, left, right)

                with mock.patch.object(screen, "_atomic_exchange", side_effect=substitute), self.assertRaisesRegex(
                    screen.UpperBoundError,
                    "participant was substituted",
                ):
                    screen._write_json_atomic(
                        path,
                        {"version": 2},
                        replace=True,
                        expected_sha256=old_digest,
                    )

    def test_cli_is_bounded_to_absolute_render_and_finalize_paths(self) -> None:
        command, args = screen.parse_outer(
            [
                "render",
                "--scene",
                "/tmp/main.blend",
                "--reference-receipt",
                "/tmp/reference.json",
                "--output-root",
                "/tmp/output",
            ]
        )
        self.assertEqual(command, "render")
        self.assertEqual(args.output_root, Path("/tmp/output"))
        command, args = screen.parse_outer(
            ["finalize", "--report", "/tmp/report.json"]
        )
        self.assertEqual(command, "finalize")
        self.assertEqual(args.report, Path("/tmp/report.json"))
        with self.assertRaisesRegex(screen.UpperBoundError, "unknown command"):
            screen.parse_outer(["probe"])

    def test_interrupted_finalization_artifacts_are_restartable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proof = root / "quality-v3-trial-00.json"
            verification = root / "quality-v3-verification-trial-00.json"
            unrelated = root / "keep.json"
            proof.write_text("partial proof")
            verification.write_text("partial verification")
            unrelated.write_text("keep")
            screen._clear_incomplete_finalization_artifacts(root, 1)
            self.assertFalse(proof.exists())
            self.assertFalse(verification.exists())
            self.assertEqual(unrelated.read_text(), "keep")

    def test_interrupted_finalization_cleanup_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.json"
            target.write_text("owner")
            proof = root / "quality-v3-trial-00.json"
            proof.symlink_to(target)
            with self.assertRaisesRegex(screen.UpperBoundError, "private regular"):
                screen._clear_incomplete_finalization_artifacts(root, 1)
            self.assertTrue(proof.is_symlink())
            self.assertEqual(target.read_text(), "owner")

    def test_source_never_calls_spatial_gate_or_authorized_publish_api(self) -> None:
        source = (HERE / "screen_spatial75_one_render_upper_bound.py").read_text()
        self.assertNotIn("gate_decoded_pair(", source)
        self.assertNotIn("gate_decoded_pair_and_publish_prepared(", source)
        self.assertNotIn("publish_prepared_after_gate(", source)
        self.assertIn("spatial._validate_prepared_seal(prepared)", source)
        self.assertIn("spatial._publish_new(destination, prepared.encoded_png)", source)
        self.assertIn('"independent_verify_commands": 0', source)


if __name__ == "__main__":
    unittest.main()
