#!/usr/bin/env python3
"""Tests for the bounded local contiguous Cycles preview sequence wrapper."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
import zlib

from PIL import Image


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_local_cycles_spec_sequence as sequence  # noqa: E402


def _write_executable(path: Path, source: str) -> None:
    path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _write_fake_agent(path: Path) -> None:
    _write_executable(
        path,
        r'''
        #!/usr/bin/env python3
        import binascii
        import hashlib
        import json
        import os
        from pathlib import Path
        import struct
        import sys
        import zlib

        def canonical(value):
            return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

        def png(width, height, index):
            color = bytes(((index * 41) % 256, 60, 90, 255))
            raw = b"".join(b"\0" + color * width for _ in range(height))
            def chunk(kind, data):
                return (struct.pack(">I", len(data)) + kind + data +
                        struct.pack(">I", binascii.crc32(kind + data) & 0xffffffff))
            return (b"\x89PNG\r\n\x1a\n" +
                    chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) +
                    chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

        request_path = Path(sys.argv[sys.argv.index("--input") + 1])
        request = json.loads(request_path.read_text())
        if os.environ.get("CX_SPEC_RENDER_CYCLES_CANDIDATE_PROFILE") != "native":
            raise SystemExit("sequence wrapper did not force the native profile")
        if os.environ.get("CX_SPEC_RENDER_CYCLES_CANDIDATE_PROFILE_SCOPE") != "native_only":
            raise SystemExit("sequence wrapper did not force the native profile scope")
        root = Path(os.environ["CX_SPEC_RENDER_CYCLES_OUTPUT_ROOT"])
        session = root / "fake-cycles-session"
        units_dir = session / "units"
        units_dir.mkdir(parents=True)
        outputs = []
        renderer = {
            "blender_build_hash": "fake-build-hash",
            "blender_version": "Fake Blender 1.0",
            "candidate_profile": {
                "name": "native",
                "max_bounces": None,
                "diffuse_bounces": None,
                "glossy_bounces": None,
                "transmission_bounces": None,
                "use_light_tree": None,
                "use_adaptive_sampling": False,
                "adaptive_min_samples": None,
                "adaptive_threshold": None,
            },
            "dependency_count": 2,
            "dependency_paths_sha256": "d" * 64,
            "device": ("GPU/METAL" if os.environ["CX_SPEC_RENDER_CYCLES_DEVICE"] == "METAL" else "CPU"),
            "enabled_device_names": ["Fake Renderer"],
            "native_integrator": {
                "max_bounces": 12,
                "diffuse_bounces": 4,
                "glossy_bounces": 4,
                "transmission_bounces": 12,
            },
            "png_compression": 0,
            "reference_sampling": {
                "use_light_tree": True,
                "use_adaptive_sampling": False,
                "adaptive_min_samples": 0,
                "adaptive_threshold": 0.01,
            },
        }
        mode = os.environ.get("FAKE_SEQUENCE_MODE", "ok")
        for index, unit in enumerate(request["units"]):
            payload = unit["payload"]
            unit_dir = units_dir / f"unit-{index:04d}"
            unit_dir.mkdir()
            artifact = unit_dir / "draft.png"
            artifact.write_bytes(png(payload["width"], payload["height"], index))
            artifact_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
            if mode == "bad-digest" and index == 0:
                artifact_sha = "0" * 64
            this_renderer = dict(renderer)
            if mode == "renderer-drift" and index == 1:
                this_renderer["blender_build_hash"] = "different-build"
            relative_artifact = artifact.relative_to(root).as_posix()
            manifest = {
                "schema_version": 2,
                "kind": "cx_cycles_preview_manifest",
                "preview_only": True,
                "billing_eligible": False,
                "production_ready": False,
                "artifact_verified": False,
                "evidence": "synthetic",
                "execution_identity_revalidation": {
                    "initial_content": "sha256",
                    "per_render": "pre_and_post_stat_identity_plus_bundle_file_set",
                },
                "unit_id": unit["unit_id"],
                "binding_sha256": hashlib.sha256(canonical({
                    "binding_policy": "render-preview-operator-policy-v2",
                    "modality": "render",
                    "operator_policy": {
                        "candidate_profile": renderer["candidate_profile"],
                        "candidate_profile_scope": "native_only",
                        "profile_authorization": "native_only",
                        "png_compression": 0,
                    },
                    "payload": payload,
                    "unit_id": unit["unit_id"],
                })).hexdigest(),
                "phase": "draft",
                "scene": {
                    "relative_path": payload["scene_path"],
                    "sha256": payload["scene_sha256"],
                    "bundle_sha256": "b" * 64,
                    "bundle_files": 2,
                    "bundle_bytes": 1234,
                },
                "render": {
                    "engine": "CYCLES",
                    "device": renderer["device"],
                    "width": payload["width"],
                    "height": payload["height"],
                    "frame": payload["frame"],
                    "samples": payload["draft_samples"],
                    "sample_offset": 0,
                    "seed": index + 7,
                    "integrator_policy": {
                        "mode": (
                            "candidate_capped"
                            if mode == "bad-integrator-policy" and index == 0
                            else "fixed_reference"
                        ),
                        "candidate_profile": renderer["candidate_profile"],
                        "candidate_profile_scope": "native_only",
                        "actual_integrator": renderer["native_integrator"],
                        "actual_sampling": renderer["reference_sampling"],
                        "samples_are_cap_when_adaptive": False,
                        "repair_and_baseline_use_reference_policy": True,
                    },
                    "pixel_filter": {"type": "BLACKMAN_HARRIS", "width": 1.5},
                    "png_compression": 0,
                    "worker_renderer_identity": this_renderer,
                },
                "artifact": {
                    "path": relative_artifact,
                    "sha256": artifact_sha,
                    "media_type": "image/png",
                },
                "pins": {
                    "blender_sha256": os.environ["CX_SPEC_RENDER_CYCLES_BLENDER_SHA256"],
                    "backend_sha256": os.environ["CX_SPEC_RENDER_PREVIEW_BACKEND_SHA256"],
                    "child_script_sha256": "c" * 64,
                    "controller_core_sha256": os.environ["CX_SPEC_RENDER_PREVIEW_CORE_SHA256"],
                    "controller_adapter_sha256": os.environ["CX_SPEC_RENDER_PREVIEW_ADAPTER_SHA256"],
                },
            }
            manifest_path = unit_dir / "draft-manifest.json"
            manifest_path.write_bytes(canonical(manifest) + b"\n")
            outputs.append({
                "schema_version": 2,
                "kind": "cx_cycles_preview_artifact",
                "manifest_path": manifest_path.relative_to(root).as_posix(),
            })
        if mode == "reverse":
            outputs.reverse()
        count = len(outputs)
        receipt = {
            "schema_version": 1,
            "draft_cost_s": 0.1,
            "verify_cost_s": 0.1,
            "accepted_fraction": 1.0,
            "repair_cost_s": 0.0,
            "overhead_cost_s": 0.01,
            "total_product_time_s": 0.21,
            "quality_tier": "preview",
            "speedup_vs_baseline": None,
            "exact": False,
            "modality": "render",
            "branch_id": "agent-render-preview-v1",
            "units": count,
            "accepted_units": count,
            "repaired_units": 0,
            "repaired_fraction": 0.0,
            "baseline_total_time_s": 0.0,
            "baseline_source": "absent",
            "quality_gate": True,
            "artifact_verified": False,
            "evidence": "synthetic",
            "global_ssim": None,
            "worst_tile_ssim": None,
            "p5_ssim": None,
            "claim_scope": "fake local preview",
            "details": {},
        }
        result = {
            "schema_version": 1,
            "kind": "cx_spec_render_preview_result",
            "preview_only": True,
            "billing_eligible": False,
            "production_ready": False,
            "receipt_trust": "local_experiment_unattested",
            "outputs": outputs,
            "receipt": receipt,
        }
        sys.stdout.buffer.write(canonical(result) + b"\n")
        ''',
    )


def _write_fake_ffmpeg(path: Path) -> None:
    _write_executable(
        path,
        r'''
        #!/usr/bin/env python3
        import json
        from pathlib import Path
        import sys

        argv = sys.argv[1:]
        if "-frames:v" in argv:
            frames = int(argv[argv.index("-frames:v") + 1])
            fps = int(argv[argv.index("-framerate") + 1])
            Path(argv[-1]).write_text(json.dumps({"frames": frames, "fps": fps}))
        elif "-progress" in argv:
            value = json.loads(Path(argv[argv.index("-i") + 1]).read_text())
            duration_us = round(value["frames"] / value["fps"] * 1_000_000)
            print(f"frame={value['frames']}")
            print(f"out_time_us={duration_us}")
            print("progress=end")
        else:
            raise SystemExit(9)
        ''',
    )


def _write_fake_ffprobe(path: Path) -> None:
    _write_executable(
        path,
        r'''
        #!/usr/bin/env python3
        import json
        from pathlib import Path
        import sys

        value = json.loads(Path(sys.argv[-1]).read_text())
        fps = value["fps"]
        duration = value["frames"] / fps
        result = {
            "streams": [{
                "index": 0,
                "codec_name": "h264",
                "codec_type": "video",
                "width": 16,
                "height": 16,
                "pix_fmt": "yuv420p",
                "r_frame_rate": f"{fps}/1",
                "avg_frame_rate": f"{fps}/1",
                "nb_read_frames": str(value["frames"]),
                "duration": f"{duration:.6f}",
            }],
            "format": {"duration": f"{duration:.6f}"},
        }
        print(json.dumps(result))
        ''',
    )


class LocalCyclesSpecSequenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.scene_root = self.root / "scenes"
        self.scene_root.mkdir()
        self.scene = self.scene_root / "shot.blend"
        self.scene.write_bytes(b"fake pinned blend")
        (self.scene_root / "texture.bin").write_bytes(b"dependency")
        self.output_root = self.root / "outputs"
        self.output_root.mkdir()
        self.agent = self.root / "cx-agent"
        _write_fake_agent(self.agent)
        self.driver = self.root / "driver.py"
        self.backend = self.root / "backend.py"
        self.core = self.root / "core.py"
        self.adapter = self.root / "adapter.py"
        self.blender = self.root / "blender"
        for path in (self.driver, self.blender):
            _write_executable(path, "#!/bin/sh\nexit 0\n")
        for path in (self.backend, self.core, self.adapter):
            path.write_text(f"# pinned {path.name}\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _args(self, *extra: str):
        values = [
            "--agent",
            str(self.agent),
            "--agent-sha256",
            sequence.sha256_file(self.agent),
            "--driver",
            str(self.driver),
            "--backend",
            str(self.backend),
            "--core",
            str(self.core),
            "--adapter",
            str(self.adapter),
            "--blender",
            str(self.blender),
            "--scene",
            str(self.scene),
            "--scene-root",
            str(self.scene_root),
            "--output-root",
            str(self.output_root),
            "--device",
            "CPU",
            "--width",
            "16",
            "--height",
            "16",
            "--frame-start",
            "3",
            "--frame-end",
            "5",
            "--draft-samples",
            "4",
            "--verify-samples",
            "4",
            "--repair-samples",
            "16",
            "--timeout-secs",
            "10",
            *extra,
        ]
        return sequence.parse_args(values)

    def test_sequence_validates_order_and_writes_content_addressed_manifest(self) -> None:
        result = sequence.run_sequence(self._args())

        self.assertTrue(result["preview_only"])
        self.assertTrue(result["local_unattested"])
        self.assertFalse(result["billing_eligible"])
        self.assertEqual(result["frame_count"], 3)
        self.assertGreater(result["agent_wall_s"], 0)
        self.assertIsNone(result["video"])
        manifest_path = Path(result["manifest_path"])
        self.assertEqual(sequence.sha256_file(manifest_path), result["manifest_sha256"])
        self.assertEqual(
            manifest_path.name, f"sequence-{result['manifest_sha256']}.json"
        )
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual([row["frame"] for row in manifest["frames"]], [3, 4, 5])
        self.assertTrue(all(row["phase"] == "draft" for row in manifest["frames"]))
        self.assertEqual(manifest["scene"]["bundle"]["sha256"], "b" * 64)
        self.assertEqual(manifest["renderer_identity"]["device"], "CPU")
        self.assertIsNone(manifest["performance_claim"])
        self.assertIsNone(manifest["speedup_vs_baseline"])
        self.assertFalse(manifest["baseline_measured"])
        self.assertIn("no artifact attestation", manifest["claim_scope"])
        self.assertGreater(manifest["execution_timing_s"]["agent_wall"], 0)
        self.assertIn("sequence_wrapper", manifest["pins"])
        self.assertGreater(
            manifest["receipt_summary"]["total_product_time_s"], 0
        )

    def test_rejects_result_reordering_digest_tamper_and_renderer_drift(self) -> None:
        for mode, pattern in (
            ("reverse", "identity/contract|render config/order"),
            ("bad-digest", "PNG SHA-256 mismatch"),
            ("renderer-drift", "one renderer identity"),
            ("bad-integrator-policy", "integrator policy"),
        ):
            with self.subTest(mode=mode):
                shutil.rmtree(self.output_root)
                self.output_root.mkdir()
                with mock.patch.dict(os.environ, {"FAKE_SEQUENCE_MODE": mode}):
                    with self.assertRaisesRegex(sequence.SequenceError, pattern):
                        sequence.run_sequence(self._args())

    def test_optional_video_is_pinned_silent_and_probed(self) -> None:
        ffmpeg = self.root / "ffmpeg"
        ffprobe = self.root / "ffprobe"
        _write_fake_ffmpeg(ffmpeg)
        _write_fake_ffprobe(ffprobe)
        video = self.root / "sequence.mp4"
        args = self._args(
            "--fps",
            "20",
            "--video-out",
            str(video),
            "--ffmpeg",
            str(ffmpeg),
            "--ffmpeg-sha256",
            sequence.sha256_file(ffmpeg),
            "--ffprobe",
            str(ffprobe),
            "--ffprobe-sha256",
            sequence.sha256_file(ffprobe),
        )

        result = sequence.run_sequence(args)
        video_row = result["video"]
        self.assertEqual(video_row["frame_count"], 3)
        self.assertEqual(video_row["fps"], 20)
        self.assertTrue(video_row["silent"])
        self.assertEqual(video_row["expected_duration_s"], 0.15)
        self.assertEqual(video_row["observed_duration_s"], 0.15)
        self.assertEqual(video_row["ffmpeg_config"]["codec"], "h264_videotoolbox")
        self.assertEqual(video_row["ffmpeg_config"]["pixel_format"], "yuv420p")
        self.assertEqual(video_row["ffmpeg_config"]["audio"], "none")
        self.assertEqual(sequence.sha256_file(video), video_row["sha256"])
        self.assertEqual(video_row["ffmpeg_sha256"], sequence.sha256_file(ffmpeg))
        self.assertEqual(video_row["ffprobe_sha256"], sequence.sha256_file(ffprobe))
        self.assertGreater(video_row["timing_s"]["total_wall"], 0)

    def test_ffprobe_rejects_audio_or_wrong_codec_contract(self) -> None:
        base = {
            "streams": [{
                "index": 0,
                "codec_name": "h264",
                "codec_type": "video",
                "width": 16,
                "height": 16,
                "pix_fmt": "yuv420p",
                "r_frame_rate": "24/1",
                "avg_frame_rate": "24/1",
                "nb_read_frames": "3",
            }],
            "format": {"duration": "0.125"},
        }
        audio = json.loads(json.dumps(base))
        audio["streams"].append({"index": 1, "codec_type": "audio"})
        with self.assertRaisesRegex(sequence.SequenceError, "exactly one stream"):
            sequence._validate_ffprobe(
                json.dumps(audio).encode(), expected_frames=3, fps=24, width=16, height=16
            )
        wrong = json.loads(json.dumps(base))
        wrong["streams"][0]["codec_name"] = "hevc"
        with self.assertRaisesRegex(sequence.SequenceError, "codec"):
            sequence._validate_ffprobe(
                json.dumps(wrong).encode(), expected_frames=3, fps=24, width=16, height=16
            )

    def test_mux_publish_race_never_deletes_concurrent_destination(self) -> None:
        ffmpeg = self.root / "ffmpeg-race"
        ffprobe = self.root / "ffprobe-race"
        _write_fake_ffmpeg(ffmpeg)
        _write_fake_ffprobe(ffprobe)
        artifact = self.output_root / "race.png"
        Image.new("RGB", (16, 16), (10, 20, 30)).save(artifact, "PNG")
        video = self.root / "raced.mp4"
        args = self._args()
        args.video_out = video
        args.fps = 24
        real_link = os.link

        def racing_link(source, destination, *, follow_symlinks=True):
            destination = Path(destination)
            if (
                destination.name == video.name
                and destination.parent.resolve() == video.parent.resolve()
            ):
                destination.write_bytes(b"owned by concurrent creator")
                raise FileExistsError("concurrent publication")
            return real_link(source, destination, follow_symlinks=follow_symlinks)

        with mock.patch.object(sequence.os, "link", side_effect=racing_link):
            with self.assertRaises(FileExistsError):
                sequence._mux_video(
                    [{
                        "_absolute_artifact_path": artifact,
                        "artifact_sha256": sequence.sha256_file(artifact),
                    }],
                    args=args,
                    output_root=self.output_root,
                    ffmpeg_pin=sequence._pin_file(
                        ffmpeg, "ffmpeg", executable=True
                    ),
                    ffprobe_pin=sequence._pin_file(
                        ffprobe, "ffprobe", executable=True
                    ),
                )
        self.assertEqual(video.read_bytes(), b"owned by concurrent creator")

    def test_content_addressed_manifest_never_clobbers(self) -> None:
        manifest = {"schema_version": 1, "kind": "fixture"}
        path, digest = sequence._write_content_addressed_manifest(
            self.output_root, manifest
        )
        original = path.read_bytes()
        with self.assertRaises(FileExistsError):
            sequence._write_content_addressed_manifest(self.output_root, manifest)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(hashlib.sha256(original).hexdigest(), digest)

    def test_rejects_unbounded_range_bad_pin_and_incomplete_mux_pins(self) -> None:
        bad_range = self._args()
        bad_range.frame_end = bad_range.frame_start + sequence.MAX_FRAMES
        with self.assertRaisesRegex(sequence.SequenceError, "frame limit"):
            sequence.validate_args(bad_range)

        bad_pin = self._args()
        bad_pin.agent_sha256 = "0" * 64
        with self.assertRaisesRegex(sequence.SequenceError, "SHA-256 mismatch"):
            sequence.run_sequence(bad_pin)

        incomplete = self._args("--video-out", str(self.root / "x.mp4"))
        with self.assertRaisesRegex(sequence.SequenceError, "supplied together"):
            sequence.validate_args(incomplete)

    @unittest.skipUnless(
        os.environ.get("CX_RUN_REAL_CYCLES_SEQUENCE_MUX_SMOKE") == "1",
        "set CX_RUN_REAL_CYCLES_SEQUENCE_MUX_SMOKE=1 for VideoToolbox mux smoke",
    )
    def test_real_videotoolbox_mux_opt_in(self) -> None:
        ffmpeg_raw = shutil.which("ffmpeg")
        ffprobe_raw = shutil.which("ffprobe")
        if not ffmpeg_raw or not ffprobe_raw:
            self.skipTest("ffmpeg/ffprobe not installed")
        ffmpeg = Path(ffmpeg_raw).resolve()
        ffprobe = Path(ffprobe_raw).resolve()
        artifacts = self.output_root / "real-pngs"
        artifacts.mkdir()
        frame_rows = []
        for index in range(3):
            path = artifacts / f"{index}.png"
            Image.new("RGB", (16, 16), (index * 50, 30, 70)).save(path, "PNG")
            frame_rows.append(
                {
                    "_absolute_artifact_path": path,
                    "artifact_sha256": sequence.sha256_file(path),
                }
            )
        args = self._args()
        args.video_out = self.root / "real.mp4"
        args.fps = 24
        result = sequence._mux_video(
            frame_rows,
            args=args,
            output_root=self.output_root,
            ffmpeg_pin=sequence._pin_file(
                ffmpeg,
                "ffmpeg",
                expected_sha256=sequence.sha256_file(ffmpeg),
                executable=True,
            ),
            ffprobe_pin=sequence._pin_file(
                ffprobe,
                "ffprobe",
                expected_sha256=sequence.sha256_file(ffprobe),
                executable=True,
            ),
        )
        self.assertEqual(result["frame_count"], 3)
        self.assertTrue(result["silent"])


if __name__ == "__main__":
    unittest.main()
