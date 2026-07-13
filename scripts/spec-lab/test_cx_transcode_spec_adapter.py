#!/usr/bin/env python3
"""Local correctness checks for the transcode SpecEngine adapter.

Most checks use SYNTHETIC fixture seconds + SSIMs. Small ffmpeg-gated regressions exercise
the real truncated/retimed-static failure modes, but make no performance claim. Real numbers
come only from `--codec ... --mode ...` runs with a real local ffmpeg.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import cx_transcode_spec_adapter as tsa  # noqa: E402

_AUTO = object()


def seg(i, *, accepted=True, draft=0.5, verify=0.1, ssim=0.98, repair=0.0,
        repair_verified=None, evidence=tsa.SYNTHETIC,
        draft_timeline_verified=_AUTO, repair_timeline_verified=_AUTO):
    if draft_timeline_verified is _AUTO:
        draft_timeline_verified = True if evidence == tsa.MEASURED else None
    if repair_timeline_verified is _AUTO:
        repair_timeline_verified = (
            True
            if evidence == tsa.MEASURED and not accepted and repair_verified is True
            else None
        )
    return tsa.SegmentResult(
        seg_id=f"seg_{i:03d}", draft_encode_s=draft, verify_s=verify,
        accepted=accepted, draft_ssim=ssim, repair_encode_s=repair, evidence=evidence,
        repair_verified=repair_verified,
        draft_timeline_verified=draft_timeline_verified,
        repair_timeline_verified=repair_timeline_verified,
    )


def direct_receipt(**overrides):
    values = {
        "branch_id": "direct",
        "modality": "transcode",
        "units": 1,
        "accepted_units": 1,
        "repaired_units": 0,
        "draft_cost_s": 0.1,
        "verify_cost_s": 0.1,
        "repair_cost_s": 0.0,
        "overhead_cost_s": 0.0,
        "baseline_total_time_s": 1.0,
        "baseline_source": "measured",
        "exact": False,
        "quality_tier": "delivery",
        "evidence": tsa.MEASURED,
    }
    values.update(overrides)
    return tsa.TranscodeSpecReceipt(**values)


def stream_compatibility(*, profile="High"):
    return tsa.StreamCompatibility(
        codec_name="h264", profile=profile, width=64, height=64,
        pix_fmt="yuv420p", level=10, time_base="1/1000",
    )


class SegmentPlanTest(unittest.TestCase):
    def test_exact_division(self):
        self.assertEqual(tsa.plan_segments(12.0, 2.0), 6)

    def test_trailing_partial_segment(self):
        self.assertEqual(tsa.plan_segments(13.0, 2.0), 7)

    def test_single_short_clip(self):
        self.assertEqual(tsa.plan_segments(1.5, 2.0), 1)

    def test_invalid_inputs_raise(self):
        for duration, segment in (
            (0.0, 2.0), (10.0, 0.0), (True, 2.0),
            (10.0, float("nan")), (float("inf"), 2.0),
        ):
            with self.subTest(duration=duration, segment=segment):
                with self.assertRaises(ValueError):
                    tsa.plan_segments(duration, segment)

    def test_runner_rejects_excessive_pixel_frame_work_before_synthesis(self):
        with self.assertRaisesRegex(ValueError, "pixel-frames"):
            tsa.run_real(
                codec="x264", mode="ssim", workdir="/tmp/cx-never-created-pixel-cap",
                duration_s=1.0, segment_time_s=1.0, size="8192x8192", fps=10,
            )

    def test_lossless_mode_rejects_lossy_codec_recipes_before_synthesis(self):
        with tempfile.TemporaryDirectory() as parent:
            workdir = os.path.join(parent, "owned-run")
            with mock.patch.object(tsa, "synthesize_source") as synthesize:
                with self.assertRaisesRegex(ValueError, "x264_lossless"):
                    tsa.run_real(
                        codec="x264", mode="lossless", workdir=workdir,
                        duration_s=1.0, segment_time_s=1.0,
                        size="64x64", fps=1,
                    )
            synthesize.assert_not_called()
            self.assertFalse(os.path.exists(workdir))

    def test_runner_cleans_owned_workspace_on_failure(self):
        with tempfile.TemporaryDirectory() as parent:
            workdir = os.path.join(parent, "owned-run")
            with mock.patch.object(tsa, "synthesize_source", side_effect=RuntimeError("boom")):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    tsa.run_real(
                        codec="x264", mode="ssim", workdir=workdir,
                        duration_s=1.0, segment_time_s=1.0,
                        size="64x64", fps=1,
                    )
            self.assertFalse(os.path.exists(workdir))

    def test_runner_cleans_owned_workspace_on_base_exceptions(self):
        for exception in (KeyboardInterrupt(), SystemExit(9)):
            with self.subTest(exception=type(exception).__name__):
                with tempfile.TemporaryDirectory() as parent:
                    workdir = os.path.join(parent, "owned-run")
                    with mock.patch.object(
                        tsa, "synthesize_source", side_effect=exception
                    ):
                        with self.assertRaises(type(exception)):
                            tsa.run_real(
                                codec="x264", mode="ssim", workdir=workdir,
                                duration_s=1.0, segment_time_s=1.0,
                                size="64x64", fps=1,
                            )
                    self.assertFalse(os.path.exists(workdir))

    def test_runner_rejects_symlink_and_shared_writable_workspaces(self):
        with tempfile.TemporaryDirectory() as parent:
            target = os.path.join(parent, "target")
            os.mkdir(target)
            link = os.path.join(parent, "link")
            os.symlink(target, link)
            with self.assertRaisesRegex(ValueError, "symlink"):
                tsa.run_real(
                    codec="x264", mode="ssim", workdir=link,
                    duration_s=1.0, segment_time_s=1.0, size="64x64", fps=1,
                )

            unsafe = os.path.join(parent, "shared")
            os.mkdir(unsafe)
            os.chmod(unsafe, 0o777)
            with self.assertRaisesRegex(ValueError, "group/world writable"):
                tsa.run_real(
                    codec="x264", mode="ssim", workdir=unsafe,
                    duration_s=1.0, segment_time_s=1.0, size="64x64", fps=1,
                )

    def test_runner_requires_strict_keep_artifacts_boolean_without_creating_dir(self):
        with tempfile.TemporaryDirectory() as parent:
            workdir = os.path.join(parent, "not-created")
            with self.assertRaises(TypeError):
                tsa.run_real(
                    codec="x264", mode="ssim", workdir=workdir,
                    duration_s=1.0, segment_time_s=1.0, size="64x64", fps=1,
                    keep_artifacts="false",
                )
            self.assertFalse(os.path.lexists(workdir))

    def test_scratch_preflight_has_headroom_above_measured_peak(self):
        pixel_frames = 100_000
        raw_bytes = (pixel_frames * 3 + 1) // 2
        required = tsa.required_scratch_bytes(pixel_frames)
        self.assertGreater(
            required,
            raw_bytes * tsa.MEASURED_RETAINED_PEAK_RAW_MULTIPLIER,
        )
        self.assertEqual(
            required,
            int(raw_bytes * tsa.SCRATCH_RAW_MULTIPLIER)
            + tsa.SCRATCH_FIXED_HEADROOM_BYTES,
        )

    def test_scratch_preflight_rejects_before_source_synthesis(self):
        pixel_frames = 64 * 64
        required = tsa.required_scratch_bytes(pixel_frames)
        with tempfile.TemporaryDirectory() as parent:
            workdir = os.path.join(parent, "owned-run")
            with mock.patch.object(
                tsa.shutil, "disk_usage", return_value=mock.Mock(free=required - 1)
            ), mock.patch.object(tsa, "synthesize_source") as synthesize:
                with self.assertRaisesRegex(ValueError, "insufficient scratch space"):
                    tsa.run_real(
                        codec="x264", mode="ssim", workdir=workdir,
                        duration_s=1.0, segment_time_s=1.0,
                        size="64x64", fps=1,
                    )
            synthesize.assert_not_called()
            self.assertFalse(os.path.exists(workdir))


class MediaIntegrityTest(unittest.TestCase):
    def test_concat_manifest_is_contained_no_clobber_and_quote_safe(self):
        with tempfile.TemporaryDirectory() as workdir:
            segment_dir = os.path.join(workdir, "seg's")
            os.mkdir(segment_dir)
            segment = os.path.join(segment_dir, "part.mkv")
            with open(segment, "wb") as fh:
                fh.write(b"fixture")
            destination = os.path.join(workdir, "out.mkv")
            with mock.patch.object(
                tsa, "_run",
                return_value=(mock.Mock(returncode=0), 0.01),
            ) as run:
                self.assertEqual(
                    tsa.concat_segments([segment], destination, workdir), 0.01
                )
            manifest = os.path.join(workdir, "concat_list.txt")
            with open(manifest, encoding="utf-8") as fh:
                row = fh.read()
            self.assertEqual(row, "file 'seg'\\''s/part.mkv'\n")
            self.assertIn(os.path.realpath(manifest), run.call_args.args[0])

        with tempfile.TemporaryDirectory() as workdir, tempfile.NamedTemporaryFile() as outside:
            with self.assertRaisesRegex(ValueError, "escapes workdir"):
                tsa.concat_segments(
                    [outside.name], os.path.join(workdir, "out.mkv"), workdir
                )

    def test_descriptor_retained_publication_is_no_clobber(self):
        with tempfile.TemporaryDirectory() as workdir:
            candidate = os.path.join(workdir, ".candidate.mkv")
            destination = os.path.join(workdir, "delivered.mkv")
            payload = b"verified-artifact" * 1024
            with open(candidate, "wb") as fh:
                fh.write(payload)
            digest, size = tsa._publish_verified_artifact(candidate, destination)
            self.assertEqual(digest, tsa.file_sha256(destination))
            self.assertEqual(size, len(payload))
            self.assertFalse(os.path.lexists(candidate))
            with open(destination, "rb") as fh:
                self.assertEqual(fh.read(), payload)

            with open(candidate, "wb") as fh:
                fh.write(b"second")
            with self.assertRaises(FileExistsError):
                tsa._publish_verified_artifact(candidate, destination)
            self.assertTrue(os.path.isfile(candidate))
            with open(destination, "rb") as fh:
                self.assertEqual(fh.read(), payload)

    @unittest.skipUnless(tsa.have_ffmpeg(), "ffmpeg+ffprobe required")
    def test_real_ffconcat_accepts_safely_escaped_quote_path(self):
        with tempfile.TemporaryDirectory() as workdir:
            segment_dir = os.path.join(workdir, "seg's")
            os.mkdir(segment_dir)
            segment = os.path.join(segment_dir, "part.mkv")
            tsa._run([
                "ffmpeg", "-y", "-nostdin", "-v", "error",
                "-f", "lavfi", "-i", "color=c=gray:s=32x32:r=2",
                "-frames:v", "2", "-c:v", "ffv1", segment,
            ])
            destination = os.path.join(workdir, "out.mkv")
            tsa.concat_segments([segment], destination, workdir)
            timeline, _ = tsa.probe_video_timeline(destination, decoder_threads=1)
            self.assertEqual(timeline.decoded_frames, 2)

    def test_normalized_cadence_cannot_hide_constant_pts_shift(self):
        reference = tsa.VideoTimeline(
            decoded_frames=3,
            start_time_s=None,
            duration_s=None,
            fps=10.0,
            first_frame_pts_s=0.0,
            last_frame_pts_s=0.2,
            normalized_pts_sha256="a" * 64,
            stream_compatibility=stream_compatibility(),
        )
        shifted = tsa.VideoTimeline(
            decoded_frames=3,
            start_time_s=None,
            duration_s=None,
            fps=10.0,
            first_frame_pts_s=0.1,
            last_frame_pts_s=0.3,
            normalized_pts_sha256="a" * 64,
            stream_compatibility=stream_compatibility(),
        )

        verified, reason, _ = tsa._compare_timelines(shifted, reference)
        self.assertFalse(verified)
        self.assertIn("first-frame PTS mismatch", reason)

    def test_invalid_timeline_numbers_fail_closed(self):
        valid = tsa.VideoTimeline(
            decoded_frames=1,
            start_time_s=None,
            duration_s=None,
            fps=10.0,
            first_frame_pts_s=0.0,
            last_frame_pts_s=0.0,
            normalized_pts_sha256="b" * 64,
            stream_compatibility=stream_compatibility(),
        )
        for invalid in (
            tsa.VideoTimeline(**{**valid.__dict__, "decoded_frames": True}),
            tsa.VideoTimeline(**{**valid.__dict__, "first_frame_pts_s": float("nan")}),
            tsa.VideoTimeline(**{**valid.__dict__, "first_frame_pts_s": "0"}),
            tsa.VideoTimeline(**{**valid.__dict__, "last_frame_pts_s": -1.0}),
            tsa.VideoTimeline(**{**valid.__dict__, "fps": float("inf")}),
            tsa.VideoTimeline(**{**valid.__dict__, "normalized_pts_sha256": "not-a-digest"}),
        ):
            with self.subTest(invalid=invalid):
                verified, reason, _ = tsa._compare_timelines(invalid, valid)
                self.assertFalse(verified)
                self.assertIn("invalid distorted timeline", reason)

    def test_worker_fanout_respects_aggregate_component_floor(self):
        self.assertEqual(tsa._bounded_worker_count(4, 8, 3, 2), 1)
        self.assertEqual(tsa._bounded_worker_count(4, 8, 4, 2), 2)
        self.assertEqual(tsa._bounded_worker_count(4, 8, 12, 2), 4)
        for invalid in (0, True, 1.5):
            with self.assertRaises(ValueError):
                tsa._bounded_worker_count(invalid, 1, 2, 2)

    def test_concat_compatibility_rejects_profile_switch(self):
        high = stream_compatibility(profile="High")
        self.assertEqual(tsa._require_concat_compatible([high, high]), high)
        with self.assertRaisesRegex(tsa.FfmpegError, "compatibility mismatch"):
            tsa._require_concat_compatible([
                stream_compatibility(profile="Constrained Baseline"), high,
            ])

    def test_frame_probe_drains_large_stderr_without_pipe_deadlock(self):
        real_popen = tsa.subprocess.Popen
        script = (
            "import sys; "
            "sys.stdout.write('0.000000\\n0.100000\\n'); "
            "sys.stdout.flush(); "
            "sys.stderr.write('x'*300000 + 'TAIL_MARKER'); "
            "sys.stderr.flush(); "
            "raise SystemExit(7)"
        )

        def high_stderr_popen(_cmd, **kwargs):
            return real_popen([sys.executable, "-c", script], **kwargs)

        with mock.patch.object(tsa.subprocess, "Popen", side_effect=high_stderr_popen):
            with self.assertRaisesRegex(tsa.FfmpegError, "TAIL_MARKER") as raised:
                tsa._probe_frame_pts_digest("fixture.mkv", decoder_threads=1)
        # Diagnostics retain only the bounded tail rather than the hostile full stream.
        self.assertLess(len(str(raised.exception)), 2500)

    def test_ssim_thread_plan_splits_total_budget_not_three_copies(self):
        for budget in range(tsa.SSIM_THREAD_COMPONENTS, 17):
            plan = tsa._ssim_thread_plan(budget)
            self.assertEqual(sum(plan), budget)
            self.assertTrue(all(value >= 1 for value in plan))
        for invalid in (1, 2, True, 3.0):
            with self.assertRaises(ValueError):
                tsa._ssim_thread_plan(invalid)

    def test_encode_thread_plan_budgets_decoder_and_encoder(self):
        for budget in range(tsa.ENCODE_THREAD_COMPONENTS, 17):
            plan = tsa._encode_thread_plan(budget)
            self.assertEqual(sum(plan), budget)
            self.assertTrue(all(value >= 1 for value in plan))
        for invalid in (0, 1, True, 2.0):
            with self.assertRaises(ValueError):
                tsa._encode_thread_plan(invalid)

    @unittest.skipUnless(tsa.have_ffmpeg(), "ffmpeg+ffprobe required")
    def test_mixed_x264_presets_concat_with_monotonic_source_timeline(self):
        with tempfile.TemporaryDirectory() as workdir:
            source = os.path.join(workdir, "source.mkv")
            seg_dir = os.path.join(workdir, "segs")
            os.makedirs(seg_dir)
            tsa.synthesize_source(
                source, duration_s=2.0, segment_time_s=1.0,
                size="64x64", fps=8, noise=0, threads=2,
            )
            source_segments, _ = tsa.split_segments(source, seg_dir, 1.0)
            self.assertEqual(len(source_segments), 2)
            delivered_segments = []
            delivered_compatibility = []
            for index, (source_segment, recipe) in enumerate(zip(
                source_segments,
                (tsa.CODECS["x264"]["fast"], tsa.CODECS["x264"]["slow"]),
                strict=True,
            )):
                delivered_segment = os.path.join(seg_dir, f"out_{index}.mkv")
                tsa.encode(source_segment, delivered_segment, recipe, threads=2)
                delivered_segments.append(delivered_segment)
                segment_timeline, _ = tsa.probe_video_timeline(
                    delivered_segment, decoder_threads=1
                )
                delivered_compatibility.append(
                    segment_timeline.stream_compatibility
                )
            compatibility = tsa._require_concat_compatible(
                delivered_compatibility
            )
            self.assertEqual(compatibility.profile, "High")
            for recipe in (
                tsa.CODECS["x264"]["fast"], tsa.CODECS["x264"]["slow"]
            ):
                self.assertIn("8x8dct=1:weightp=0:repeat-headers=1", recipe)
            delivered = os.path.join(workdir, "delivered.mkv")
            tsa.concat_segments(delivered_segments, delivered, workdir)
            delivered_timeline, _ = tsa.probe_video_timeline(
                delivered, decoder_threads=1
            )
            source_timeline, _ = tsa.probe_video_timeline(source, decoder_threads=1)

            verified, reason, _ = tsa._compare_timelines(
                delivered_timeline, source_timeline
            )
            self.assertTrue(verified, reason)

    @unittest.skipUnless(tsa.have_ffmpeg(), "ffmpeg+ffprobe required")
    def test_truncated_static_clip_cannot_pass_ssim(self):
        with tempfile.TemporaryDirectory() as workdir:
            reference = os.path.join(workdir, "reference.mkv")
            truncated = os.path.join(workdir, "truncated.mkv")
            for path, frames in ((reference, 12), (truncated, 4)):
                tsa._run([
                    "ffmpeg", "-y", "-nostdin", "-v", "error",
                    "-f", "lavfi", "-i", "color=c=gray:s=64x64:r=12",
                    "-frames:v", str(frames), "-c:v", "ffv1", path,
                ])
            verification = tsa.verify_ssim(
                truncated, reference, threads=tsa.SSIM_THREAD_COMPONENTS
            )
            self.assertFalse(verification.timeline_verified)
            self.assertEqual(verification.score, 0.0)
            self.assertIn("decoded-frame count mismatch", verification.timeline_reason)
            self.assertEqual(verification.distorted_timeline.decoded_frames, 4)
            self.assertEqual(verification.reference_timeline.decoded_frames, 12)

    @unittest.skipUnless(tsa.have_ffmpeg(), "ffmpeg+ffprobe required")
    def test_lossless_md5_cannot_bless_retimed_static_clip(self):
        with tempfile.TemporaryDirectory() as workdir:
            reference = os.path.join(workdir, "reference.mkv")
            retimed = os.path.join(workdir, "retimed.mkv")
            tsa._run([
                "ffmpeg", "-y", "-nostdin", "-v", "error",
                "-f", "lavfi", "-i", "color=c=gray:s=64x64:r=12",
                "-frames:v", "12", "-c:v", "ffv1", reference,
            ])
            tsa._run([
                "ffmpeg", "-y", "-nostdin", "-v", "error", "-i", reference,
                "-vf", "setpts=2*PTS", "-fps_mode", "passthrough",
                "-c:v", "ffv1", retimed,
            ])
            reference_md5, _ = tsa.decoded_md5(reference, threads=1)
            retimed_md5, _ = tsa.decoded_md5(retimed, threads=1)
            self.assertEqual(retimed_md5, reference_md5)
            verification = tsa.verify_decoded_exact(retimed, reference, threads=1)
            self.assertFalse(verification.exact)
            self.assertFalse(verification.timeline_verified)
            self.assertEqual(
                verification.distorted_timeline.decoded_frames,
                verification.reference_timeline.decoded_frames,
            )
            self.assertIn("cadence/PTS mismatch", verification.timeline_reason)

    @unittest.skipUnless(tsa.have_ffmpeg(), "ffmpeg+ffprobe required")
    def test_same_count_and_duration_variable_cadence_is_rejected(self):
        with tempfile.TemporaryDirectory() as workdir:
            reference = os.path.join(workdir, "reference.mkv")
            variable = os.path.join(workdir, "variable.mkv")
            tsa._run([
                "ffmpeg", "-y", "-nostdin", "-v", "error",
                "-f", "lavfi", "-i", "color=c=gray:s=64x64:r=12",
                "-frames:v", "12", "-c:v", "ffv1", reference,
            ])
            # The sine perturbation is zero at N=0 and N=11, preserving start/end and
            # frame count while changing only the internal cadence.
            tsa._run([
                "ffmpeg", "-y", "-nostdin", "-v", "error", "-i", reference,
                "-vf", "settb=1/1000,setpts=PTS+0.02*sin(N*PI/11)/TB",
                "-fps_mode", "passthrough", "-enc_time_base", "1/1000",
                "-c:v", "ffv1", variable,
            ])
            reference_md5, _ = tsa.decoded_md5(reference, threads=1)
            variable_md5, _ = tsa.decoded_md5(variable, threads=1)
            self.assertEqual(variable_md5, reference_md5)
            reference_timeline, _ = tsa.probe_video_timeline(
                reference, decoder_threads=1
            )
            variable_timeline, _ = tsa.probe_video_timeline(
                variable, decoder_threads=1
            )
            self.assertEqual(
                variable_timeline.decoded_frames, reference_timeline.decoded_frames
            )
            self.assertAlmostEqual(
                variable_timeline.duration_s, reference_timeline.duration_s, places=3
            )
            self.assertNotEqual(
                variable_timeline.normalized_pts_sha256,
                reference_timeline.normalized_pts_sha256,
            )
            verification = tsa.verify_decoded_exact(variable, reference, threads=1)
            self.assertFalse(verification.exact)
            self.assertIn("cadence/PTS mismatch", verification.timeline_reason)


class AccountingTest(unittest.TestCase):
    def test_accept_repair_costs_and_one_ratio_headline(self):
        # 4 accepted + 1 rejected->repaired. Fixture seconds.
        segments = [seg(i) for i in range(4)] + [
            seg(4, accepted=False, ssim=0.91, repair=4.0, repair_verified=True)
        ]
        rec = tsa.build_receipt(
            segments, baseline_wall_s=25.0, segment_wall_s=0.2, concat_wall_s=0.1,
            exact=False, baseline_source="modeled", delivery_verified=True,
        )
        self.assertEqual(rec.units, 5)
        self.assertEqual(rec.accepted_units, 4)
        self.assertEqual(rec.repaired_units, 1)
        self.assertAlmostEqual(rec.accepted_fraction, 0.8)
        self.assertAlmostEqual(rec.repaired_fraction, 0.2)
        # draft cost carries the FULL cheap path: segmentation + drafts + concat
        self.assertAlmostEqual(rec.draft_cost_s, 0.2 + 5 * 0.5 + 0.1)
        self.assertAlmostEqual(rec.verify_cost_s, 5 * 0.1)
        self.assertAlmostEqual(rec.repair_cost_s, 4.0)  # only the rejected segment
        self.assertAlmostEqual(rec.total_product_time_s, 2.8 + 0.5 + 4.0)
        # headline is ONE ratio baseline/total — never a product of per-segment ratios
        self.assertAlmostEqual(rec.speedup_vs_baseline, 25.0 / 7.3, places=6)

    def test_repair_disabled_ships_preview_not_delivery(self):
        segments = [seg(0), seg(1, accepted=False, ssim=0.90, repair=4.0)]
        rec = tsa.build_receipt(
            segments, baseline_wall_s=10.0, segment_wall_s=0.1, concat_wall_s=0.1,
            exact=False, repair_enabled=False, baseline_source="modeled",
        )
        self.assertEqual(rec.quality_tier, "preview")  # below-gate draft was shipped
        self.assertFalse(rec.artifact_verified)
        self.assertEqual(rec.repaired_units, 0)
        self.assertAlmostEqual(rec.repair_cost_s, 0.0)  # repair never charged when disabled

    def test_all_accepted_is_delivery_with_zero_repair(self):
        rec = tsa.build_receipt(
            [seg(i) for i in range(3)], baseline_wall_s=9.0, segment_wall_s=0.1,
            concat_wall_s=0.1, exact=False, baseline_source="modeled", delivery_verified=True,
        )
        self.assertEqual(rec.quality_tier, "delivery")
        self.assertAlmostEqual(rec.repair_cost_s, 0.0)
        self.assertAlmostEqual(rec.repaired_fraction, 0.0)

    def test_measured_verified_delivery_sets_product_proof(self):
        rec = tsa.build_receipt(
            [seg(i, evidence=tsa.MEASURED) for i in range(2)],
            baseline_wall_s=9.0, segment_wall_s=0.1, concat_wall_s=0.1,
            exact=False, baseline_source="measured", delivery_verified=True,
        )
        self.assertEqual(rec.quality_tier, "delivery")
        self.assertTrue(rec.artifact_verified)
        self.assertTrue(rec.to_dict()["artifact_verified"])

    def test_direct_measured_delivery_does_not_infer_artifact_proof(self):
        rec = direct_receipt()
        self.assertFalse(rec.artifact_verified)
        self.assertFalse(rec.to_dict()["artifact_verified"])
        self.assertTrue(direct_receipt(artifact_verified=True).artifact_verified)
        with self.assertRaisesRegex(ValueError, "artifact_verified=true"):
            direct_receipt(artifact_verified=True, evidence=tsa.MODELED)
        with self.assertRaisesRegex(ValueError, "artifact_verified=true"):
            direct_receipt(artifact_verified=True, quality_tier="fail")

    def test_direct_receipt_rejects_scalar_identity_and_empty_time_forgery(self):
        for overrides in (
            {"units": True},
            {"accepted_units": True},
            {"repaired_units": True},
            {"draft_cost_s": True},
            {"verify_cost_s": False},
            {"units": 0, "accepted_units": 0},
            {
                "draft_cost_s": 0.0, "verify_cost_s": 0.0,
                "repair_cost_s": 0.0, "overhead_cost_s": 0.0,
            },
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                direct_receipt(**overrides)
        with self.assertRaisesRegex(ValueError, "UTF-8 bytes"):
            direct_receipt(branch_id="é" * 129)
        with self.assertRaisesRegex(ValueError, "UTF-8 bytes"):
            tsa.SegmentResult(
                seg_id="é" * 129, draft_encode_s=0.1, verify_s=0.1,
                accepted=True,
            )
        with self.assertRaisesRegex(ValueError, "draft_bytes"):
            tsa.SegmentResult(
                seg_id="s", draft_encode_s=0.1, verify_s=0.1,
                accepted=True, draft_bytes=True,
            )

    def test_measured_segments_require_explicit_timeline_proof(self):
        accepted_without_proof = seg(
            0, evidence=tsa.MEASURED, draft_timeline_verified=None
        )
        rec = tsa.build_receipt(
            [accepted_without_proof], baseline_wall_s=1.0,
            segment_wall_s=0.1, concat_wall_s=0.1, exact=False,
            baseline_source="measured", delivery_verified=True,
        )
        self.assertEqual(rec.quality_tier, "fail")
        self.assertFalse(rec.artifact_verified)
        self.assertFalse(rec.details["delivery_verified"])
        self.assertEqual(rec.details["invalid_accepted_timeline_proofs"], 1)

        repaired_without_proof = seg(
            1, accepted=False, repair=0.5, repair_verified=True,
            evidence=tsa.MEASURED, repair_timeline_verified=None,
        )
        rec = tsa.build_receipt(
            [repaired_without_proof], baseline_wall_s=1.0,
            segment_wall_s=0.1, concat_wall_s=0.1, exact=False,
            baseline_source="measured", delivery_verified=True,
        )
        self.assertEqual(rec.quality_tier, "fail")
        self.assertEqual(rec.repaired_units, 0)
        self.assertFalse(rec.artifact_verified)

    def test_proof_accounting_details_cannot_be_overridden_by_caller(self):
        rec = tsa.build_receipt(
            [seg(0)], baseline_wall_s=1.0, segment_wall_s=0.1,
            concat_wall_s=0.1, exact=False, baseline_source="modeled",
            delivery_verified=False,
            details={
                "delivery_verified": True,
                "unresolved_failed_segments": -7,
                "final_verify_wall_s": -9,
                "cost_note": "forged",
                "repair_note": "forged",
            },
        )
        self.assertFalse(rec.details["delivery_verified"])
        self.assertEqual(rec.details["unresolved_failed_segments"], 0)
        self.assertEqual(rec.details["final_verify_wall_s"], 0.0)
        self.assertNotEqual(rec.details["cost_note"], "forged")
        self.assertNotEqual(rec.details["repair_note"], "forged")

    def test_repaired_to_baseline_recipe_is_delivery(self):
        rec = tsa.build_receipt(
            [seg(0, accepted=False, ssim=0.5, repair=3.0, repair_verified=True)],
            baseline_wall_s=5.0, segment_wall_s=0.1, concat_wall_s=0.1, exact=False,
            baseline_source="modeled", delivery_verified=True,
        )
        # mirrors the render lane: a repaired unit is delivered at the baseline recipe
        self.assertEqual(rec.quality_tier, "delivery")
        self.assertAlmostEqual(rec.repaired_fraction, 1.0)

    def test_empty_delivery_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "at least one segment"):
            tsa.build_receipt(
                [], baseline_wall_s=5.0, segment_wall_s=0.0, concat_wall_s=0.0,
                exact=False, baseline_source="modeled",
            )

    def test_speedup_null_when_baseline_absent_or_zero_total(self):
        rec = tsa.build_receipt(
            [seg(0)], baseline_wall_s=0.0, segment_wall_s=0.1, concat_wall_s=0.1,
            exact=False, baseline_source="absent",
        )
        self.assertIsNone(rec.speedup_vs_baseline)  # a speedup is never fabricated
        d = rec.to_dict()
        self.assertIsNone(d["speedup_vs_baseline"])

    def test_receipt_is_bounded_and_nonabsent_baseline_is_positive(self):
        with self.assertRaises(ValueError):
            tsa.build_receipt(
                [seg(0)], baseline_wall_s=0.0, segment_wall_s=0.1,
                concat_wall_s=0.1, exact=False, baseline_source="measured",
            )
        with mock.patch.object(tsa, "MAX_TRANSCODE_RECEIPT_UNITS", 2):
            with self.assertRaisesRegex(ValueError, "safety cap"):
                tsa.build_receipt(
                    (seg(i) for i in range(3)), baseline_wall_s=5.0,
                    segment_wall_s=0.1, concat_wall_s=0.1, exact=False,
                    baseline_source="modeled",
                )

    def test_near_unit_cap_receipt_is_compact_and_under_ingress_limit(self):
        rec = tsa.build_receipt(
            (seg(i) for i in range(tsa.MAX_TRANSCODE_RECEIPT_UNITS)),
            baseline_wall_s=5.0, segment_wall_s=0.1, concat_wall_s=0.1,
            exact=False, baseline_source="modeled", delivery_verified=True,
        )
        wire = rec.to_dict()
        tsa.assert_canonical(wire)
        encoded = tsa._canonical_json_bytes(wire)
        self.assertLessEqual(len(encoded), tsa.MAX_RECEIPT_JSON_BYTES)
        self.assertEqual(
            wire["details"]["per_segment_summary"]["count"],
            tsa.MAX_TRANSCODE_RECEIPT_UNITS,
        )
        self.assertEqual(
            len(wire["details"]["per_segment"]),
            tsa.MAX_PER_SEGMENT_DETAIL_SAMPLES,
        )
        self.assertEqual(
            wire["details"]["per_segment_summary"]["sample_strategy"],
            "first+last",
        )

    def test_oversized_caller_details_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "caller details"):
            tsa.build_receipt(
                [seg(0)], baseline_wall_s=5.0, segment_wall_s=0.1,
                concat_wall_s=0.1, exact=False, baseline_source="modeled",
                details={"blob": "x" * tsa.MAX_CALLER_DETAILS_JSON_BYTES},
            )

    def test_deeply_nested_caller_details_are_rejected_before_ingress(self):
        details = {}
        # Caller details live one level below the receipt root, so depth 32 here would
        # produce canonical depth 33 and must fail before Rust ingress sees it.
        for _ in range(tsa.MAX_RECEIPT_JSON_DEPTH - 1):
            details = {"nested": details}
        self.assertEqual(
            tsa._json_nesting_depth(details), tsa.MAX_RECEIPT_JSON_DEPTH
        )
        with self.assertRaisesRegex(ValueError, "nesting depth"):
            tsa.build_receipt(
                [seg(0)], baseline_wall_s=5.0, segment_wall_s=0.1,
                concat_wall_s=0.1, exact=False, baseline_source="modeled",
                details=details,
            )

    def test_repair_enabled_without_verified_artifact_fails_closed(self):
        rec = tsa.build_receipt(
            [seg(0, accepted=False, ssim=0.1, repair=0.0, repair_verified=None)],
            baseline_wall_s=5.0, segment_wall_s=0.1, concat_wall_s=0.1,
            exact=False, repair_enabled=True, delivery_verified=True,
            baseline_source="measured",
        )
        self.assertEqual(rec.quality_tier, "fail")
        self.assertEqual(rec.repaired_units, 0)
        self.assertEqual(rec.details["unresolved_failed_segments"], 1)

    def test_final_artifact_verification_is_required_for_delivery(self):
        rec = tsa.build_receipt(
            [seg(0)], baseline_wall_s=5.0, segment_wall_s=0.1, concat_wall_s=0.1,
            exact=False, delivery_verified=False, baseline_source="modeled",
        )
        self.assertEqual(rec.quality_tier, "fail")

    def test_evidence_worst_label_dominates(self):
        segments = [seg(0, evidence=tsa.MEASURED), seg(1, evidence=tsa.SYNTHETIC)]
        rec = tsa.build_receipt(
            segments, baseline_wall_s=5.0, segment_wall_s=0.1, concat_wall_s=0.1,
            exact=False, baseline_source="modeled",
        )
        self.assertEqual(rec.evidence, tsa.SYNTHETIC)  # dirtiest unit wins
        segments = [seg(0, evidence=tsa.MEASURED), seg(1, evidence=tsa.MODELED)]
        rec = tsa.build_receipt(
            segments, baseline_wall_s=5.0, segment_wall_s=0.1, concat_wall_s=0.1,
            exact=False, baseline_source="modeled",
        )
        self.assertEqual(rec.evidence, tsa.MODELED)

    def test_ssim_gated_is_never_exact_unless_proven(self):
        # `exact` comes ONLY from the caller's decoded-frame MD5 proof, never from SSIM.
        rec = tsa.build_receipt(
            [seg(0, ssim=1.0)], baseline_wall_s=5.0, segment_wall_s=0.1, concat_wall_s=0.1,
            exact=False, baseline_source="modeled",
        )
        self.assertFalse(rec.to_dict()["exact"])
        self.assertIn("NOT lossless", rec.to_dict()["claim_scope"])
        proven = direct_receipt(exact=True, artifact_verified=True).to_dict()
        self.assertIn("proven exact", proven["claim_scope"])


class CanonicalShapeTest(unittest.TestCase):
    """The emitted dict must satisfy spec-engine/src/receipt.rs's deserializer — the Python
    mimic of serde strictness (required keys, enum vocab, numeric/bool types)."""

    def _receipt_dict(self):
        segments = [seg(i) for i in range(4)] + [
            seg(4, accepted=False, ssim=0.91, repair=4.0, repair_verified=True)
        ]
        return tsa.build_receipt(
            segments, baseline_wall_s=25.0, segment_wall_s=0.2, concat_wall_s=0.1,
            exact=False, baseline_source="modeled", delivery_verified=True,
        ).to_dict()

    def test_required_and_defaulted_receipt_rs_fields_present(self):
        d = self._receipt_dict()
        tsa.assert_canonical(d)
        for k in tsa.CANONICAL_REQUIRED_FIELDS + tsa.CANONICAL_DEFAULTED_FIELDS:
            self.assertIn(k, d)

    def test_enum_values_are_receipt_rs_vocab(self):
        d = self._receipt_dict()
        self.assertIn(d["quality_tier"], tsa.QUALITY_TIERS)
        self.assertIn(d["evidence"], tsa.EVIDENCE_WIRE)
        self.assertIn(d["baseline_source"], tsa.BASELINE_SOURCES)
        self.assertEqual(d["modality"], "transcode")
        self.assertIsInstance(d["exact"], bool)
        self.assertIsInstance(d["artifact_verified"], bool)
        self.assertIsInstance(d["units"], int)

    def test_total_is_sum_of_charged_parts(self):
        d = self._receipt_dict()
        self.assertAlmostEqual(
            d["total_product_time_s"],
            d["draft_cost_s"] + d["verify_cost_s"] + d["repair_cost_s"]
            + d["overhead_cost_s"], places=4,
        )

    def test_parallel_map_preserves_source_order(self):
        self.assertEqual(tsa._parallel_map(lambda x: x * x, [3, 1, 2], 3), [9, 1, 4])

    def test_json_roundtrip_value_stable(self):
        d = self._receipt_dict()
        self.assertEqual(json.loads(json.dumps(d)), d)

    def test_assert_canonical_rejects_missing_and_bad_values(self):
        d = self._receipt_dict()
        for k in tsa.CANONICAL_REQUIRED_FIELDS:
            broken = dict(d)
            del broken[k]
            with self.assertRaises(AssertionError, msg=f"missing {k} must fail"):
                tsa.assert_canonical(broken)
        bad_tier = dict(d, quality_tier="g>=0.98,wt>=0.95")  # a gate SPEC is not a tier enum
        with self.assertRaises(AssertionError):
            tsa.assert_canonical(bad_tier)
        bad_evidence = dict(d, evidence="MEASURED")  # wire form must be lower-case
        with self.assertRaises(AssertionError):
            tsa.assert_canonical(bad_evidence)
        bad_exact = dict(d, exact="true")  # must be a real bool
        with self.assertRaises(AssertionError):
            tsa.assert_canonical(bad_exact)
        hidden_cost = dict(d, total_product_time_s=d["total_product_time_s"] + 1.0)
        with self.assertRaises(AssertionError):
            tsa.assert_canonical(hidden_cost)  # nothing may hide outside draft+verify+repair

    def test_simulate_is_synthetic_and_canonical(self):
        d = tsa.simulate()
        tsa.assert_canonical(d)
        self.assertEqual(d["evidence"], "synthetic")   # never mistakable for a measurement
        self.assertEqual(d["baseline_source"], "modeled")
        self.assertEqual(d["quality_tier"], "delivery")
        self.assertFalse(d["artifact_verified"])      # synthetic fixture cannot ship
        self.assertFalse(d["exact"])                   # SSIM-gated fixture: not lossless

    def test_per_segment_transparency_in_details(self):
        d = self._receipt_dict()
        per = d["details"]["per_segment"]
        self.assertEqual(len(per), 5)
        rejected = [p for p in per if not p["accepted"]]
        self.assertEqual(len(rejected), 1)
        self.assertAlmostEqual(rejected[0]["repair_encode_s"], 4.0)


if __name__ == "__main__":
    unittest.main()
