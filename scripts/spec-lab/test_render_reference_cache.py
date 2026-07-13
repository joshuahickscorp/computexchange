#!/usr/bin/env python3
"""Focused integrity tests for exp_render_stack's hardware-bound ref cache."""

import copy
import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
POD = os.path.join(HERE, "pod")
sys.path.insert(0, POD)

import exp_render_stack as stack  # noqa: E402


def identity(cpu="Apple M3 Pro", physical="Apple M3 Pro"):
    return {
        "schema": stack.REF_CACHE_IDENTITY_SCHEMA,
        "hardware": {
            "system": "Darwin",
            "release": "25.5.0",
            "machine": "arm64",
            "cpu_model": cpu,
            "product_model": "Mac15,6" if "Pro" in cpu else "Mac15,14",
            "memory_bytes": 36 << 30,
        },
        "blender": {
            "identity_kind": "blender_build_hash",
            "version": [4, 2, 0],
            "version_string": "4.2.0 LTS",
            "version_cycle": "release",
            "build_platform": "Darwin arm64",
            "build_hash": "deadbeef1234",
            "build_branch": "blender-v4.2-release",
            "build_commit_date": "2024-07-16",
            "build_commit_time": "06:20",
            "executable_sha256": "ab" * 32,
            "executable_size": 123456,
        },
        "blender_runtime": {
            "python_version": [3, 11, 9],
            "python_implementation": "CPython",
            "blender_binary_name": "Blender",
        },
        "device": {
            "render_device": "GPU/METAL",
            "physical_devices": [{
                "name": physical,
                "type": "METAL",
            }],
        },
        "accelerator_runtime": {"kind": "metal_os", "version": "15.5"},
    }


def scene_bundle(digest="11" * 32):
    return {
        "schema": stack.REF_CACHE_INPUT_SCHEMA,
        "main_blend": "classroom/classroom.blend",
        "file_count": 2,
        "total_bytes": 100,
        "sha256": digest,
    }


def implementation(digest="22" * 32):
    return {
        "schema": stack.REF_CACHE_INPUT_SCHEMA,
        "kind": "blender_scene_script",
        "sha256": digest,
    }


def cache_key(cache_identity):
    return stack.make_reference_cache_key(
        scene_key="classroom", res_x=960, res_y=540, ref_spp=512,
        bounces=12, frames=2, seed=0, cam_motion=1.0,
        identity=cache_identity,
        scene_bundle=scene_bundle(), implementation=implementation(),
    )


def manifest(cache_identity):
    return {
        "schema": stack.REF_CACHE_SCHEMA,
        "key": cache_key(cache_identity),
        "frames": 2,
        "per_frame_ref_s": [10.25, 9.75],
        "devices": ["GPU/METAL"],
        "cache_identity": copy.deepcopy(cache_identity),
        "frame_artifacts": [{
            "schema": stack.REF_CACHE_ARTIFACT_SCHEMA,
            "index": index,
            "filename": f"color_{index:04d}.npy",
            "shape": [2, 3, 3],
            "dtype": "float32",
            "finite": True,
            "byte_size": 200,
            "npy_sha256": "33" * 32,
        } for index in range(2)],
    }


class CacheKeyBindingTest(unittest.TestCase):
    def test_m3_pro_and_m3_ultra_have_different_cache_addresses(self):
        pro = identity()
        ultra = identity(cpu="Apple M3 Ultra", physical="Apple M3 Ultra")
        self.assertNotEqual(cache_key(pro), cache_key(ultra))

    def test_blender_build_and_physical_device_change_cache_address(self):
        base = identity()
        other_build = copy.deepcopy(base)
        other_build["blender"]["build_hash"] = "cafefeed5678"
        other_gpu = copy.deepcopy(base)
        other_gpu["device"]["physical_devices"][0]["name"] = "Apple M3 Ultra"
        self.assertNotEqual(cache_key(base), cache_key(other_build))
        self.assertNotEqual(cache_key(base), cache_key(other_gpu))

    def test_scene_bundle_and_reference_implementation_change_address(self):
        base = identity()
        common = dict(
            scene_key="classroom", res_x=960, res_y=540, ref_spp=512,
            bounces=12, frames=2, seed=0, cam_motion=1.0, identity=base,
        )
        first = stack.make_reference_cache_key(
            **common, scene_bundle=scene_bundle(), implementation=implementation())
        changed_scene = stack.make_reference_cache_key(
            **common, scene_bundle=scene_bundle("44" * 32),
            implementation=implementation())
        changed_code = stack.make_reference_cache_key(
            **common, scene_bundle=scene_bundle(),
            implementation=implementation("55" * 32))
        self.assertNotEqual(first, changed_scene)
        self.assertNotEqual(first, changed_code)


class BuildIdentityTest(unittest.TestCase):
    def test_stable_build_hash_is_also_bound_to_executable_bytes(self):
        with tempfile.NamedTemporaryFile() as binary:
            binary.write(b"actual blender bytes")
            binary.flush()
            os.chmod(binary.name, os.stat(binary.name).st_mode | stat.S_IXUSR)
            hasher = mock.Mock(return_value="cd" * 32)
            got = stack.blender_build_identity(
                binary.name,
                {
                    "version": [4, 2, 0],
                    "version_string": "4.2.0",
                    "version_cycle": "release",
                    "build_platform": "Darwin arm64",
                    "build_hash": "88559c00cd36",
                    "build_branch": "blender-v4.2-release",
                    "build_commit_date": "2024-07-16",
                    "build_commit_time": "06:20",
                },
                file_hasher=hasher,
            )
        self.assertEqual(got["identity_kind"], "blender_build_hash")
        self.assertEqual(got["build_hash"], "88559c00cd36")
        self.assertEqual(got["executable_sha256"], "cd" * 32)
        hasher.assert_called_once()

    def test_missing_build_hash_falls_back_to_executable_sha256(self):
        with tempfile.NamedTemporaryFile() as binary:
            binary.write(b"fake blender binary")
            binary.flush()
            os.chmod(binary.name, os.stat(binary.name).st_mode | stat.S_IXUSR)
            hasher = mock.Mock(return_value="ab" * 32)
            got = stack.blender_build_identity(
                binary.name,
                {"version": [4, 2, 0], "build_hash": "Unknown"},
                file_hasher=hasher,
            )
        self.assertEqual(got["identity_kind"], "binary_sha256")
        self.assertEqual(got["binary_sha256"], "ab" * 32)
        self.assertEqual(got["executable_sha256"], "ab" * 32)
        hasher.assert_called_once()


class HostAndRuntimeProbeTest(unittest.TestCase):
    def test_blender_identity_probe_script_compiles(self):
        compile(stack.BLENDER_CACHE_IDENTITY_SCRIPT, "<cache-identity>", "exec")

    def _darwin_identity(self, chip, model):
        values = {
            "machdep.cpu.brand_string": chip,
            "hw.model": model,
            "hw.memsize": str(36 << 30),
            "hw.physicalcpu": "12",
            "hw.logicalcpu": "12",
        }

        def command(argv, timeout_s=10):
            del timeout_s
            return values[argv[-1]]

        with (
            mock.patch.object(stack.platform, "system", return_value="Darwin"),
            mock.patch.object(stack.platform, "machine", return_value="arm64"),
            mock.patch.object(stack.platform, "release", return_value="25.5.0"),
            mock.patch.object(stack, "_command_value", side_effect=command),
        ):
            return stack.host_hardware_identity()

    def test_darwin_identity_uses_chip_class_not_just_arm64(self):
        pro = self._darwin_identity("Apple M3 Pro", "Mac15,6")
        ultra = self._darwin_identity("Apple M3 Ultra", "Mac15,14")
        self.assertNotEqual(pro, ultra)
        self.assertEqual(pro["cpu_model"], "Apple M3 Pro")
        self.assertEqual(ultra["cpu_model"], "Apple M3 Ultra")
        self.assertEqual(pro["physical_cpus"], 12)
        self.assertEqual(pro["logical_cpus"], 12)

    def test_blender_probe_preserves_physical_device_name(self):
        payload = {
            "build": {"version": [4, 2, 0], "build_hash": "deadbeef"},
            "runtime": {
                "python_version": [3, 11, 9],
                "blender_binary_name": "Blender",
            },
            "device": {
                "render_device": "GPU/METAL",
                "physical_devices": [{
                    "name": "Apple M3 Ultra", "type": "METAL",
                }],
            },
        }
        proc = SimpleNamespace(
            returncode=0,
            stdout="Blender 4.2\nCX_CACHE_RUNTIME_IDENTITY=" + json.dumps(payload) + "\n",
            stderr="",
        )
        with mock.patch.object(stack.subprocess, "run", return_value=proc) as run:
            got = stack.probe_blender_runtime("/Applications/Blender", "GPU")
        self.assertEqual(
            got["device"]["physical_devices"][0]["name"], "Apple M3 Ultra"
        )
        self.assertEqual(run.call_args.kwargs["env"]["CX_DEVICE"], "GPU")

    def test_unknown_or_backend_mismatched_gpu_identity_is_rejected(self):
        for bad in (
            {"render_device": "GPU/METAL", "physical_devices": [
                {"name": "Unknown GPU", "type": "METAL"}]},
            {"render_device": "GPU/METAL", "physical_devices": [
                {"name": "Apple M3 Ultra", "type": "unknown"}]},
            {"render_device": "GPU/METAL", "physical_devices": [
                {"name": "Apple M3 Ultra", "type": "CUDA"}]},
        ):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                stack.validate_device_identity(bad)


class SceneAndImplementationIdentityTest(unittest.TestCase):
    def test_dependency_bytes_are_bound_to_scene_bundle(self):
        with tempfile.TemporaryDirectory() as root:
            blend = os.path.join(root, "scene.blend")
            texture = os.path.join(root, "wall.png")
            with open(blend, "wb") as stream:
                stream.write(b"blend bytes")
            with open(texture, "wb") as stream:
                stream.write(b"texture version one")
            before = stack.scene_bundle_identity(blend)
            with open(texture, "wb") as stream:
                stream.write(b"texture version two")
            after = stack.scene_bundle_identity(blend)
        self.assertNotEqual(before["sha256"], after["sha256"])
        self.assertEqual(before["file_count"], 2)

    def test_scene_bundle_rejects_symlinked_dependencies(self):
        with tempfile.TemporaryDirectory() as root:
            blend = os.path.join(root, "scene.blend")
            target = os.path.join(root, "real.png")
            with open(blend, "wb") as stream:
                stream.write(b"blend")
            with open(target, "wb") as stream:
                stream.write(b"pixels")
            os.symlink(target, os.path.join(root, "linked.png"))
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                stack.scene_bundle_identity(blend)

    def test_scene_script_digest_is_exact(self):
        a = stack.reference_implementation_identity("print('a')")
        b = stack.reference_implementation_identity("print('b')")
        self.assertNotEqual(a["sha256"], b["sha256"])
        self.assertEqual(
            a["sha256"], hashlib.sha256(b"print('a')").hexdigest())

    def test_materialized_scene_script_is_exclusive_and_digest_pinned(self):
        content = "print('pinned')\n"
        digest = hashlib.sha256(content.encode()).hexdigest()
        with tempfile.TemporaryDirectory() as root:
            first = stack.write_private_pinned_text(
                root, "scene", content, digest)
            second = stack.write_private_pinned_text(
                root, "scene", content, digest)
            with open(first, encoding="utf-8") as stream:
                loaded = stream.read()
        self.assertNotEqual(first, second)
        self.assertEqual(loaded, content)


class CacheArtifactIntegrityTest(unittest.TestCase):
    shape = (2, 3, 3)

    def _base_manifest(self, bound):
        return {
            "schema": stack.REF_CACHE_SCHEMA,
            "key": cache_key(bound),
            "frames": 2,
            "per_frame_ref_s": [10.25, 9.75],
            "devices": ["GPU/METAL"],
            "cache_identity": copy.deepcopy(bound),
        }

    def _publish(self, root, *, colors=None, timings=None):
        bound = identity()
        cache_dir = os.path.join(root, "address")
        if colors is None:
            colors = [
                np.full(self.shape, 0.25, dtype=np.float32),
                np.full(self.shape, 0.75, dtype=np.float32),
            ]
        m = self._base_manifest(bound)
        if timings is not None:
            m["per_frame_ref_s"] = timings
        result = stack.publish_reference_cache(
            cache_dir, colors, m, expected_shape=self.shape)
        return bound, cache_dir, result

    def _load(self, cache_dir, bound):
        return stack.load_reference_cache(
            cache_dir, key=cache_key(bound), frames=2, identity=bound,
            expected_shape=self.shape,
        )

    def _rewrite_frame_and_manifest(self, cache_dir, array):
        frame = os.path.join(cache_dir, "color_0000.npy")
        with open(frame, "wb") as stream:
            np.save(stream, array, allow_pickle=False)
        manifest_path = os.path.join(cache_dir, "manifest.json")
        with open(manifest_path, encoding="utf-8") as stream:
            m = json.load(stream)
        m["frame_artifacts"][0]["byte_size"] = os.path.getsize(frame)
        with open(frame, "rb") as stream:
            m["frame_artifacts"][0]["npy_sha256"] = hashlib.sha256(
                stream.read()).hexdigest()
        with open(manifest_path, "w", encoding="utf-8") as stream:
            json.dump(m, stream)

    def test_published_cache_round_trips_with_digest_and_dtype(self):
        with tempfile.TemporaryDirectory() as root:
            bound, cache_dir, (published, published_manifest) = self._publish(root)
            loaded_manifest, colors = self._load(cache_dir, bound)
        self.assertTrue(published)
        self.assertEqual(loaded_manifest, published_manifest)
        self.assertEqual(colors[0].dtype, np.dtype("float32"))
        self.assertTrue(loaded_manifest["frame_artifacts"][0]["npy_sha256"])

    def test_digest_tamper_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            bound, cache_dir, _ = self._publish(root)
            with open(os.path.join(cache_dir, "color_0000.npy"), "ab") as stream:
                stream.write(b"tamper")
            with self.assertRaisesRegex(ValueError, "byte size mismatch|digest mismatch"):
                self._load(cache_dir, bound)

    def test_float64_and_nonfinite_arrays_are_rejected_even_with_matching_digest(self):
        for array, message in (
            (np.zeros(self.shape, dtype=np.float64), "dtype"),
            (np.full(self.shape, np.nan, dtype=np.float32), "non-finite"),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as root:
                bound, cache_dir, _ = self._publish(root)
                self._rewrite_frame_and_manifest(cache_dir, array)
                with self.assertRaisesRegex(ValueError, message):
                    self._load(cache_dir, bound)

    def test_symlinked_npy_is_rejected_without_following(self):
        with tempfile.TemporaryDirectory() as root:
            bound, cache_dir, _ = self._publish(root)
            frame = os.path.join(cache_dir, "color_0000.npy")
            target = os.path.join(root, "outside.npy")
            os.rename(frame, target)
            os.symlink(target, frame)
            with self.assertRaises(OSError):
                self._load(cache_dir, bound)

    def test_complete_concurrent_winner_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as root:
            bound, cache_dir, _ = self._publish(root, timings=[1.0, 2.0])
            other = [np.ones(self.shape, dtype=np.float32) for _ in range(2)]
            _bound, _cache_dir, (published, winner) = self._publish(
                root, colors=other, timings=[90.0, 91.0])
            loaded, colors = self._load(cache_dir, bound)
        self.assertFalse(published)
        self.assertEqual(winner["per_frame_ref_s"], [1.0, 2.0])
        self.assertEqual(loaded["per_frame_ref_s"], [1.0, 2.0])
        self.assertAlmostEqual(float(colors[0][0, 0, 0]), 0.25)

    def test_invalid_publication_is_quarantined_before_atomic_replacement(self):
        with tempfile.TemporaryDirectory() as root:
            bound, cache_dir, _ = self._publish(root)
            with open(os.path.join(cache_dir, "color_0000.npy"), "ab") as stream:
                stream.write(b"broken")
            other = [np.ones(self.shape, dtype=np.float32) for _ in range(2)]
            _bound, _cache_dir, (published, _winner) = self._publish(
                root, colors=other)
            _manifest, colors = self._load(cache_dir, bound)
            quarantines = [name for name in os.listdir(root) if ".invalid-" in name]
        self.assertTrue(published)
        self.assertTrue(quarantines)
        self.assertAlmostEqual(float(colors[0][0, 0, 0]), 1.0)

    def test_exclusive_writer_refuses_preexisting_symlink(self):
        with tempfile.TemporaryDirectory() as root:
            victim = os.path.join(root, "victim")
            link = os.path.join(root, "temp")
            with open(victim, "w", encoding="utf-8") as stream:
                stream.write("safe")
            os.symlink(victim, link)
            with self.assertRaises(FileExistsError):
                stack._open_exclusive_nofollow(link)
            with open(victim, encoding="utf-8") as stream:
                self.assertEqual(stream.read(), "safe")


class ManifestFailClosedTest(unittest.TestCase):
    def test_valid_v2_manifest_is_accepted(self):
        bound = identity()
        self.assertTrue(stack.validate_reference_cache_manifest(
            manifest(bound), key=cache_key(bound), frames=2, identity=bound,
        ))

    def test_legacy_manifest_without_identity_is_rejected(self):
        legacy = {
            "key": "v1|classroom|960|540|512|12|2|0|1.0",
            "frames": 2,
            "per_frame_ref_s": [10.0, 10.0],
            "devices": ["GPU/METAL"],
        }
        with self.assertRaisesRegex(ValueError, "schema"):
            stack.validate_reference_cache_manifest(
                legacy, key=legacy["key"], frames=2, identity=identity(),
            )

    def test_identity_or_actual_device_mismatch_is_rejected(self):
        pro = identity()
        m = manifest(pro)
        m["cache_identity"]["hardware"]["cpu_model"] = "Apple M3 Ultra"
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            stack.validate_reference_cache_manifest(
                m, key=cache_key(pro), frames=2, identity=pro,
            )

        m = manifest(pro)
        m["devices"] = ["CPU"]
        with self.assertRaisesRegex(ValueError, "actual reference render device"):
            stack.validate_reference_cache_manifest(
                m, key=cache_key(pro), frames=2, identity=pro,
            )

    def test_invalid_historical_timing_is_rejected(self):
        bound = identity()
        for bad in ([10.0], [10.0, float("nan")], [10.0, 0.0], [10.0, True]):
            with self.subTest(bad=bad):
                m = manifest(bound)
                m["per_frame_ref_s"] = bad
                with self.assertRaisesRegex(ValueError, "timing"):
                    stack.validate_reference_cache_manifest(
                        m, key=cache_key(bound), frames=2, identity=bound,
                    )

    def test_production_render_must_confirm_probed_physical_device(self):
        expected = identity()["device"]
        self.assertTrue(stack.validate_actual_render_device(
            expected, copy.deepcopy(expected)
        ))
        wrong = copy.deepcopy(expected)
        wrong["physical_devices"][0]["name"] = "Apple M3 Ultra"
        with self.assertRaisesRegex(RuntimeError, "disagreed"):
            stack.validate_actual_render_device(expected, wrong)
        with self.assertRaisesRegex(RuntimeError, "omitted"):
            stack.validate_actual_render_device(expected, None)


if __name__ == "__main__":
    unittest.main()
