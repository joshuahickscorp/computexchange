#!/usr/bin/env python3
"""Adversarial and opt-in real tests for the pinned Cycles preview backend."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from PIL import Image, ImageChops


HERE = Path(__file__).resolve().parent
BACKEND_PATH = HERE / "cx_cycles_render_preview_backend.py"
DRIVER_PATH = HERE / "cx_agent_render_preview_driver.py"
sys.path.insert(0, str(HERE))

import cx_agent_render_preview_driver as driver  # noqa: E402


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_payload(scene: Path, **updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "scene_path": scene.name,
        "scene_sha256": sha(scene),
        "width": 64,
        "height": 64,
        "frame": 1,
        "draft_samples": 4,
        "verify_samples": 4,
        "repair_samples": 16,
    }
    payload.update(updates)
    return payload


def make_request(
    payload: dict[str, object],
    *,
    unit_id: str = "frame-1",
    unit_meta: dict[str, object] | None = None,
) -> bytes:
    return json.dumps({
        "schema_version": 1,
        "kind": driver.REQUEST_KIND,
        "units": [{
            "unit_id": unit_id,
            "payload": payload,
            "meta": {} if unit_meta is None else unit_meta,
        }],
        "meta": {"job": "cycles-backend-test"},
    }).encode("utf-8")


def write_fake_blender(path: Path, mode: str) -> None:
    """Create a pinned executable renderer using only the Python stdlib."""
    source = r'''#!__PYTHON__
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import time
import zlib

MODE = __MODE__
PROTOCOL = "cx-cycles-preview-worker-v1"
MAX_FRAME_BYTES = 16 << 10
RESIDENT_POLICY_BROAD = "broad_v1"
RESIDENT_POLICY_SAME_FRAME_MINIMAL = "same_frame_minimal_v1"
def profile(name, maximum, diffuse, glossy, transmission, *, light_tree=None,
            adaptive=False, adaptive_min=None, adaptive_threshold=None):
    return {
        "name": name, "max_bounces": maximum, "diffuse_bounces": diffuse,
        "glossy_bounces": glossy, "transmission_bounces": transmission,
        "use_light_tree": light_tree, "use_adaptive_sampling": adaptive,
        "adaptive_min_samples": adaptive_min,
        "adaptive_threshold": adaptive_threshold,
    }
CANDIDATE_PROFILES = {
    "native": profile("native", None, None, None, None),
    "cap16_v1": profile("cap16_v1", 16, 8, 8, 16),
    "cap12_v1": profile("cap12_v1", 12, 6, 6, 12),
    "cap8_v1": profile("cap8_v1", 8, 4, 4, 8),
    "cap8_lighttree_v1": profile("cap8_lighttree_v1", 8, 4, 4, 8, light_tree=True),
    "cap8_adaptive_v1": profile("cap8_adaptive_v1", 8, 4, 4, 8, adaptive=True, adaptive_min=8, adaptive_threshold=0.01),
    "cap8_both_v1": profile("cap8_both_v1", 8, 4, 4, 8, light_tree=True, adaptive=True, adaptive_min=8, adaptive_threshold=0.01),
    "cap8_both_relaxed_v1": profile("cap8_both_relaxed_v1", 8, 4, 4, 8, light_tree=True, adaptive=True, adaptive_min=8, adaptive_threshold=0.02),
    "oidn_native_v1": profile("oidn_native_v1", None, None, None, None),
}
NATIVE_INTEGRATOR = {"max_bounces": 32, "diffuse_bounces": 16, "glossy_bounces": 16, "transmission_bounces": 32}
REFERENCE_SAMPLING = {"use_light_tree": False, "use_adaptive_sampling": False, "adaptive_min_samples": 0, "adaptive_threshold": 0.01}
DENOISING_OFF_POLICY = {
    "use_denoising": False, "view_layer_use_denoising": False,
    "denoiser": None, "denoising_input_passes": None,
    "denoising_prefilter": None, "denoising_quality": None,
    "denoising_use_gpu": None,
}
OIDN_NATIVE_POLICY = {
    "use_denoising": True, "view_layer_use_denoising": True,
    "denoiser": "OPENIMAGEDENOISE",
    "denoising_input_passes": "RGB_ALBEDO_NORMAL",
    "denoising_prefilter": "ACCURATE", "denoising_quality": "HIGH",
    "denoising_use_gpu": False,
}
CANDIDATE_DENOISING_POLICIES = {
    name: dict(OIDN_NATIVE_POLICY if name == "oidn_native_v1" else DENOISING_OFF_POLICY)
    for name in CANDIDATE_PROFILES
}

def chunk(kind, data):
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))

def pixel_color(x, y, phase, border):
    if border is not None:
        left, top, right, bottom = border
        if not (left <= x < right and top <= y < bottom):
            return (0, 0, 0)
    if MODE in {"selective", "selective_escalate"}:
        localized = 32 <= x < 64 and 32 <= y < 64
        base = (30, 60, 90)
        truth = (255, 255, 255) if localized else base
        if phase == "draft":
            return base
        if phase == "verify":
            return truth
        if phase == "repair" and border is not None and MODE == "selective_escalate":
            return (0, 0, 0)
        return truth
    if MODE == "repair":
        if phase == "draft":
            return (0, 0, 0)
        if phase == "verify":
            return (255, 255, 255)
    return (30, 60, 90)

def write_png(path, width, height, phase, border):
    rows = []
    for y in range(height):
        row = bytearray(b"\x00")
        for x in range(width):
            row.extend(pixel_color(x, y, phase, border))
        rows.append(bytes(row))
    raw = b"".join(rows)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    Path(path).write_bytes(data)

def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")

def emit(writer, value):
    writer.write(canonical(value) + b"\n")
    writer.flush()

args = sys.argv[sys.argv.index("--") + 1:]
if len(args) != 4:
    raise SystemExit(91)
command_fd = int(args[0], 10)
response_fd = int(args[1], 10)
nonce = args[2]
scene_path = str(Path(sys.argv[sys.argv.index("--python") - 1]).resolve(strict=True))

with (
    os.fdopen(command_fd, "rb", buffering=0) as reader,
    os.fdopen(response_fd, "wb", buffering=0) as writer,
):
    candidate_profile_name = os.environ.get(
        "CX_CYCLES_CANDIDATE_PROFILE", "native"
    )
    resident_policy = os.environ.get(
        "CX_CYCLES_RESIDENT_POLICY", RESIDENT_POLICY_BROAD
    )
    ready = {
        "blender_build_hash": "fixture",
        "blender_version": "fixture",
        "candidate_denoising_policy": CANDIDATE_DENOISING_POLICIES[
            candidate_profile_name
        ],
        "candidate_profile": CANDIDATE_PROFILES[candidate_profile_name],
        "dependency_count": 0,
        "dependency_paths_sha256": hashlib.sha256(canonical([])).hexdigest(),
        "png_compression": 0,
        "device": (
            (
                "CPU"
                if os.environ.get("CX_CYCLES_DEVICE") == "METAL"
                else "GPU/METAL"
            )
            if MODE == "bad_device"
            else ("GPU/METAL" if os.environ.get("CX_CYCLES_DEVICE") == "METAL" else "CPU")
        ),
        "enabled_device_names": [
            "Fixture Metal" if os.environ.get("CX_CYCLES_DEVICE") == "METAL" else "CPU"
        ],
        "kind": "ready",
        "native_integrator": NATIVE_INTEGRATOR,
        "nonce": ("0" * 64 if MODE == "bad_handshake" else nonce),
        "protocol": PROTOCOL,
        "reference_denoising_policy": DENOISING_OFF_POLICY,
        "reference_sampling": REFERENCE_SAMPLING,
        "resident_policy": (
            RESIDENT_POLICY_BROAD
            if MODE == "bad_resident_handshake" else resident_policy
        ),
        "scene_path": scene_path,
        "schema_version": 1,
    }
    if MODE == "missing_denoising_attestation":
        ready.pop("candidate_denoising_policy")
    emit(writer, ready)
    last_successful_frame = None
    while True:
        raw = reader.readline(MAX_FRAME_BYTES + 1)
        if not raw:
            break
        frame = json.loads(raw.decode("utf-8"))
        cfg = frame["command"]
        if cfg["candidate_denoising_policy"] != CANDIDATE_DENOISING_POLICIES[
            candidate_profile_name
        ]:
            raise SystemExit(93)
        if cfg["resident_policy"] != resident_policy:
            raise SystemExit(94)
        expected_digest = hashlib.sha256(canonical(cfg)).hexdigest()
        if frame["command_sha256"] != expected_digest:
            raise SystemExit(92)
        phase = cfg["phase"]
        if MODE == "fail_verify" and phase == "verify":
            raise SystemExit(17)
        if MODE == "early_exit":
            raise SystemExit(18)
        if MODE == "sleep" and phase == "draft":
            time.sleep(10)
        if MODE == "invalid_png":
            Path(cfg["output"]).write_bytes(b"not a png")
        else:
            width = cfg["width"] + (1 if MODE == "wrong_size" else 0)
            height = cfg["height"]
            write_png(cfg["output"], width, height, phase, cfg["border"])
        if MODE == "malformed_response":
            writer.write(b"{not-json}\n")
            writer.flush()
            continue
        if MODE == "oversized_response":
            writer.write(b"x" * (MAX_FRAME_BYTES + 1) + b"\n")
            writer.flush()
            continue
        response_id = cfg["command_id"] + (1 if MODE == "stale_id" else 0)
        response_digest = (
            "f" * 64 if MODE == "digest_mismatch" else expected_digest
        )
        actual_integrator = dict(NATIVE_INTEGRATOR)
        if phase in {"draft", "verify"}:
            actual_integrator = {
                key: min(NATIVE_INTEGRATOR[key], cfg["candidate_profile"][key])
                if cfg["candidate_profile"][key] is not None
                else NATIVE_INTEGRATOR[key]
                for key in NATIVE_INTEGRATOR
            }
        if MODE == "wrong_integrator":
            actual_integrator["max_bounces"] += 1
        actual_sampling = dict(REFERENCE_SAMPLING)
        if phase in {"draft", "verify"}:
            profile_row = cfg["candidate_profile"]
            if profile_row["use_light_tree"] is not None:
                actual_sampling["use_light_tree"] = profile_row["use_light_tree"]
            if profile_row["use_adaptive_sampling"]:
                actual_sampling.update({
                    "use_adaptive_sampling": True,
                    "adaptive_min_samples": profile_row["adaptive_min_samples"],
                    "adaptive_threshold": struct.unpack(
                        "f", struct.pack("f", profile_row["adaptive_threshold"])
                    )[0],
                })
        if MODE == "wrong_sampling":
            actual_sampling["use_light_tree"] = not actual_sampling["use_light_tree"]
        actual_denoising = dict(DENOISING_OFF_POLICY)
        if phase in {"draft", "verify"}:
            actual_denoising = dict(
                CANDIDATE_DENOISING_POLICIES[candidate_profile_name]
            )
        if MODE == "wrong_denoising":
            actual_denoising["use_denoising"] = not actual_denoising["use_denoising"]
        resident_mutation = (
            RESIDENT_POLICY_SAME_FRAME_MINIMAL
            if resident_policy == RESIDENT_POLICY_SAME_FRAME_MINIMAL
            and last_successful_frame == cfg["frame"]
            else RESIDENT_POLICY_BROAD
        )
        last_successful_frame = cfg["frame"]
        emit(writer, {
            "command_id": response_id,
            "command_sha256": response_digest,
            "denoising": actual_denoising,
            "integrator": actual_integrator,
            "kind": "render_result",
            "ok": True,
            "resident_mutation": (
                (
                    RESIDENT_POLICY_SAME_FRAME_MINIMAL
                    if resident_mutation == RESIDENT_POLICY_BROAD
                    else RESIDENT_POLICY_BROAD
                )
                if MODE == "wrong_resident_mutation"
                else resident_mutation
            ),
            "resident_policy": (
                RESIDENT_POLICY_BROAD
                if MODE == "wrong_resident_response" else resident_policy
            ),
            "sampling": actual_sampling,
            "schema_version": 1,
        })
'''
    source = source.replace("__PYTHON__", str(sys.executable), 1)
    source = source.replace("__MODE__", repr(mode), 1)
    path.write_text(source, encoding="utf-8")
    path.chmod(0o700)


class CyclesBackendFixture(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.scene_root = self.root / "scenes"
        self.output_root = self.root / "outputs"
        self.scene_root.mkdir()
        self.output_root.mkdir()
        self.scene = self.scene_root / "tiny.blend"
        self.scene.write_bytes(b"mock blend bytes\x00with a stable pin")

    def environment(self, mode: str = "accept") -> dict[str, str]:
        fake = self.root / f"fake-blender-{mode}"
        write_fake_blender(fake, mode)
        return {
            driver.BACKEND_ENV: str(BACKEND_PATH),
            driver.BACKEND_SHA_ENV: sha(BACKEND_PATH),
            driver.CORE_SHA_ENV: sha(driver.CORE_PATH),
            driver.ADAPTER_SHA_ENV: sha(driver.ADAPTER_PATH),
            "CX_SPEC_RENDER_CYCLES_BLENDER": str(fake),
            "CX_SPEC_RENDER_CYCLES_BLENDER_SHA256": sha(fake),
            "CX_SPEC_RENDER_CYCLES_SCENE_ROOT": str(self.scene_root),
            "CX_SPEC_RENDER_CYCLES_OUTPUT_ROOT": str(self.output_root),
            "CX_SPEC_RENDER_CYCLES_TIMEOUT_SECS": "5",
            "CX_SPEC_RENDER_CYCLES_DEVICE": "CPU",
            "HOME": str(self.root),
        }

    def execute(
        self,
        mode: str = "accept",
        *,
        candidate_profile: str | None = None,
        candidate_profile_scope: str | None = None,
        candidate_profile_auth: str | None = None,
        resident_policy: str | None = None,
        **updates: object,
    ) -> dict[str, object]:
        environment = self.environment(mode)
        if candidate_profile is not None:
            environment["CX_SPEC_RENDER_CYCLES_CANDIDATE_PROFILE"] = candidate_profile
        if candidate_profile_scope is not None:
            environment[
                "CX_SPEC_RENDER_CYCLES_CANDIDATE_PROFILE_SCOPE"
            ] = candidate_profile_scope
        if candidate_profile_auth is not None:
            environment[
                "CX_SPEC_RENDER_CYCLES_CANDIDATE_PROFILE_AUTH"
            ] = candidate_profile_auth
        if resident_policy is not None:
            environment[
                "CX_SPEC_RENDER_CYCLES_RESIDENT_POLICY"
            ] = resident_policy
        payload = make_payload(self.scene, **updates)
        loaded: list[object] = []
        real_load = driver._load_backend

        def capture_backend(*args, **kwargs):
            module = real_load(*args, **kwargs)
            loaded.append(module)
            return module

        try:
            with (
                mock.patch.dict(os.environ, environment, clear=False),
                mock.patch.object(driver, "_load_backend", side_effect=capture_backend),
            ):
                return driver.execute_request(
                    make_request(
                        payload,
                        unit_id=(
                            "local-metal-benchmark"
                            if candidate_profile_auth is not None
                            else "frame-1"
                        ),
                        unit_meta=(
                            {
                                "cx_benchmark_profile_auth_v1": (
                                    candidate_profile_auth
                                )
                            }
                            if candidate_profile_auth is not None
                            else None
                        ),
                    )
                )
        finally:
            for module in loaded:
                module._shutdown_worker()

    def load_backend(self, environment: dict[str, str]):
        with mock.patch.dict(os.environ, environment, clear=False):
            module = driver._load_backend(BACKEND_PATH, sha(BACKEND_PATH))
        self.addCleanup(module._shutdown_worker)
        return module

    def manifest_for(self, output: dict[str, object]) -> tuple[Path, dict[str, object]]:
        descriptor = output["outputs"][0]
        self.assertEqual(descriptor["kind"], "cx_cycles_preview_artifact")
        relative = Path(descriptor["manifest_path"])
        self.assertFalse(relative.is_absolute())
        path = (self.output_root / relative).resolve(strict=True)
        path.relative_to(self.output_root.resolve(strict=True))
        return path, json.loads(path.read_text(encoding="utf-8"))

    def rendered_phases(self) -> set[str]:
        return {
            json.loads(path.read_text(encoding="utf-8"))["phase"]
            for path in self.output_root.rglob("*-render-config.json")
        }

    def test_agreeing_independent_drafts_are_accepted_as_stable_manifest(self):
        output = self.execute("accept")
        manifest_path, manifest = self.manifest_for(output)

        self.assertEqual(manifest["phase"], "draft")
        self.assertEqual(manifest["render"]["device"], "CPU")
        self.assertEqual(manifest["render"]["png_compression"], 0)
        self.assertEqual(manifest["render"]["resident_policy"], "broad_v1")
        self.assertEqual(manifest["render"]["resident_mutation"], "broad_v1")
        self.assertEqual(
            manifest["render"]["integrator_policy"]["mode"], "fixed_reference"
        )
        self.assertEqual(
            manifest["render"]["worker_renderer_identity"]["png_compression"], 0
        )
        self.assertEqual(
            manifest["render"]["worker_renderer_identity"]["resident_policy"],
            "broad_v1",
        )
        self.assertEqual(self.rendered_phases(), {"draft", "verify"})
        self.assertEqual(output["receipt"]["accepted_units"], 1)
        self.assertEqual(output["receipt"]["repaired_units"], 0)
        self.assertEqual(output["receipt"]["baseline_source"], "absent")
        self.assertIsNone(output["receipt"]["speedup_vs_baseline"])
        self.assertFalse(output["receipt"]["artifact_verified"])
        self.assertEqual(output["receipt"]["evidence"], "synthetic")

        verification = json.loads(
            (manifest_path.parent / "verification-manifest.json").read_text()
        )
        self.assertTrue(verification["accepted"])
        self.assertTrue(verification["independent_seed"])
        self.assertTrue(verification["sample_ranges_disjoint"])
        self.assertEqual(verification["draft_sample_offset"], 0)
        self.assertEqual(verification["verify_sample_offset"], 4)
        self.assertNotEqual(verification["draft_seed"], verification["verify_seed"])
        self.assertEqual(verification["global_agreement"], 1.0)
        self.assertEqual(verification["worst_tile_agreement"], 1.0)
        configs = {
            json.loads(path.read_text())["phase"]: json.loads(path.read_text())
            for path in manifest_path.parent.glob("*-render-config.json")
        }
        self.assertEqual(configs["draft"]["sample_offset"], 0)
        self.assertEqual(configs["verify"]["sample_offset"], 4)
        self.assertEqual(configs["draft"]["candidate_profile"]["name"], "native")
        self.assertTrue(
            all(row["resident_policy"] == "broad_v1" for row in configs.values())
        )
        scene_copies = list(self.output_root.rglob("tiny.blend"))
        self.assertEqual(len(scene_copies), 1)
        self.assertEqual(scene_copies[0].read_bytes(), self.scene.read_bytes())
        self.assertEqual(manifest["scene"]["bundle_files"], 1)
        self.assertEqual(manifest["scene"]["bundle_bytes"], len(self.scene.read_bytes()))
        self.assertEqual(len(manifest["scene"]["bundle_sha256"]), 64)

    def test_operator_candidate_profile_is_bound_and_buyer_cannot_select_it(self):
        module = self.load_backend(self.environment("accept"))
        compile(module._BLENDER_CHILD_SOURCE, "<fixed_cycles_child>", "exec")
        benchmark_auth = "a" * 64
        with self.assertRaisesRegex(RuntimeError, "benchmark-screen scope"):
            self.execute("accept", candidate_profile="cap16_v1")
        with self.assertRaisesRegex(RuntimeError, "benchmark capability"):
            self.execute(
                "accept",
                candidate_profile="cap16_v1",
                candidate_profile_scope="benchmark_screen_v1",
            )
        output = self.execute(
            "accept",
            candidate_profile="cap16_v1",
            candidate_profile_scope="benchmark_screen_v1",
            candidate_profile_auth=benchmark_auth,
        )
        manifest_path, manifest = self.manifest_for(output)
        expected = {
            "name": "cap16_v1",
            "max_bounces": 16,
            "diffuse_bounces": 8,
            "glossy_bounces": 8,
            "transmission_bounces": 16,
            "use_light_tree": None,
            "use_adaptive_sampling": False,
            "adaptive_min_samples": None,
            "adaptive_threshold": None,
        }
        self.assertEqual(
            manifest["render"]["integrator_policy"],
            {
                "mode": "candidate_capped",
                "candidate_profile": expected,
                "candidate_profile_scope": "benchmark_screen_v1",
                "actual_integrator": {
                    "max_bounces": 16,
                    "diffuse_bounces": 8,
                    "glossy_bounces": 8,
                    "transmission_bounces": 16,
                },
                "actual_sampling": {
                    "use_light_tree": False,
                    "use_adaptive_sampling": False,
                    "adaptive_min_samples": 0,
                    "adaptive_threshold": 0.01,
                },
                "samples_are_cap_when_adaptive": False,
                "repair_and_baseline_use_reference_policy": True,
            },
        )
        self.assertEqual(
            manifest["render"]["worker_renderer_identity"]["candidate_profile"],
            expected,
        )
        configs = [
            json.loads(path.read_text())
            for path in manifest_path.parent.glob("*-render-config.json")
        ]
        self.assertTrue(configs)
        self.assertTrue(all(row["candidate_profile"] == expected for row in configs))

        repaired = self.execute(
            "repair",
            candidate_profile="cap16_v1",
            candidate_profile_scope="benchmark_screen_v1",
            candidate_profile_auth=benchmark_auth,
        )
        _repair_path, repair_manifest = self.manifest_for(repaired)
        self.assertEqual(
            repair_manifest["render"]["integrator_policy"]["mode"],
            "fixed_reference",
        )
        self.assertEqual(
            repair_manifest["repair_strategy"]["reason"],
            "non_native_profile_requires_full_reference_repair",
        )
        self.assertEqual(
            repair_manifest["repair_strategy"]["attempted"], "full_frame"
        )

        adaptive = self.execute(
            "accept",
            candidate_profile="cap8_both_v1",
            candidate_profile_scope="benchmark_screen_v1",
            candidate_profile_auth=benchmark_auth,
        )
        _adaptive_path, adaptive_manifest = self.manifest_for(adaptive)
        adaptive_policy = adaptive_manifest["render"]["integrator_policy"]
        self.assertEqual(
            adaptive_policy["actual_sampling"],
            {
                "use_light_tree": True,
                "use_adaptive_sampling": True,
                "adaptive_min_samples": 8,
                "adaptive_threshold": 0.009999999776482582,
            },
        )
        self.assertTrue(adaptive_policy["samples_are_cap_when_adaptive"])

        payload = make_payload(self.scene)
        payload["candidate_profile"] = "oidn_native_v1"
        environment = self.environment("accept")
        with mock.patch.dict(os.environ, environment, clear=False):
            with self.assertRaisesRegex(Exception, "must contain exactly"):
                driver.execute_request(make_request(payload))

    def test_private_resident_policy_is_gated_bound_and_attested(self):
        policy = "same_frame_minimal_v1"
        benchmark_auth = "e" * 64
        with self.assertRaisesRegex(RuntimeError, "benchmark-screen scope"):
            self.execute("accept", resident_policy=policy)
        with self.assertRaisesRegex(RuntimeError, "benchmark capability"):
            self.execute(
                "accept",
                resident_policy=policy,
                candidate_profile_scope="benchmark_screen_v1",
            )

        broad_output = self.execute("accept")
        _broad_path, broad_manifest = self.manifest_for(broad_output)
        output = self.execute(
            "accept",
            resident_policy=policy,
            candidate_profile_scope="benchmark_screen_v1",
            candidate_profile_auth=benchmark_auth,
        )
        manifest_path, manifest = self.manifest_for(output)
        self.assertNotEqual(
            broad_manifest["binding_sha256"], manifest["binding_sha256"]
        )
        self.assertEqual(manifest["render"]["resident_policy"], policy)
        self.assertEqual(manifest["render"]["resident_mutation"], "broad_v1")
        self.assertEqual(
            manifest["render"]["worker_renderer_identity"]["resident_policy"],
            policy,
        )
        configs = [
            json.loads(path.read_text())
            for path in manifest_path.parent.glob("*-render-config.json")
        ]
        self.assertTrue(configs)
        self.assertTrue(all(row["resident_policy"] == policy for row in configs))

        payload = make_payload(self.scene)
        payload["resident_policy"] = policy
        with mock.patch.dict(os.environ, self.environment("accept"), clear=False):
            with self.assertRaisesRegex(Exception, "must contain exactly"):
                driver.execute_request(make_request(payload))

        policy_args = {
            "resident_policy": policy,
            "candidate_profile_scope": "benchmark_screen_v1",
            "candidate_profile_auth": benchmark_auth,
        }
        with self.assertRaisesRegex(RuntimeError, "handshake identity mismatch"):
            self.execute("bad_resident_handshake", **policy_args)
        with self.assertRaisesRegex(RuntimeError, "response identity mismatch"):
            self.execute("wrong_resident_response", **policy_args)
        with self.assertRaisesRegex(RuntimeError, "mutation attestation mismatch"):
            self.execute("wrong_resident_mutation", **policy_args)

    def test_same_frame_minimal_command_sequence_keeps_seed_ranges_disjoint(self):
        policy = "same_frame_minimal_v1"
        benchmark_auth = "f" * 64
        environment = self.environment("accept")
        environment.update(
            {
                "CX_SPEC_RENDER_CYCLES_CANDIDATE_PROFILE_SCOPE": (
                    "benchmark_screen_v1"
                ),
                "CX_SPEC_RENDER_CYCLES_CANDIDATE_PROFILE_AUTH": benchmark_auth,
                "CX_SPEC_RENDER_CYCLES_RESIDENT_POLICY": policy,
            }
        )
        module = self.load_backend(environment)
        meta = {"cx_benchmark_profile_auth_v1": benchmark_auth}
        with mock.patch.dict(os.environ, environment, clear=False):
            first_unit = module.SpecUnit(
                "local-metal-benchmark",
                "render",
                make_payload(self.scene, frame=1),
                meta,
            )
            first = module._context_for_unit(first_unit)
            module._invoke_blender(
                first,
                "draft",
                4,
                first["seeds"]["draft"],
                first["unit_dir"] / "sequence-first.png",
                execution_label="sequence-first",
            )
            module._invoke_blender(
                first,
                "verify",
                4,
                first["seeds"]["verify"],
                first["unit_dir"] / "sequence-same.png",
                execution_label="sequence-same",
            )
            second_unit = module.SpecUnit(
                "local-metal-benchmark",
                "render",
                make_payload(self.scene, frame=2),
                meta,
            )
            second = module._context_for_unit(second_unit)
            module._invoke_blender(
                second,
                "draft",
                4,
                second["seeds"]["draft"],
                second["unit_dir"] / "sequence-new-frame.png",
                execution_label="sequence-new-frame",
            )

        self.assertEqual(
            [row["mutation"] for row in first["resident_mutation_history"]],
            ["broad_v1", policy],
        )
        self.assertEqual(
            [row["mutation"] for row in second["resident_mutation_history"]],
            ["broad_v1"],
        )
        self.assertIn(policy, module._worker_key(first))
        first_configs = {
            path.stem: json.loads(path.read_text())
            for path in first["unit_dir"].glob("sequence-*-render-config.json")
        }
        draft = first_configs["sequence-first-render-config"]
        verify = first_configs["sequence-same-render-config"]
        self.assertEqual(draft["sample_offset"], 0)
        self.assertEqual(verify["sample_offset"], 4)
        self.assertNotEqual(draft["seed"], verify["seed"])
        self.assertTrue(
            all(row["resident_policy"] == policy for row in first_configs.values())
        )

    def test_oidn_profile_is_pinned_and_reference_phases_are_denoise_off(self):
        benchmark_auth = "d" * 64
        profile_environment = {
            "candidate_profile": "oidn_native_v1",
            "candidate_profile_scope": "benchmark_screen_v1",
            "candidate_profile_auth": benchmark_auth,
        }
        with self.assertRaisesRegex(RuntimeError, "benchmark-screen scope"):
            self.execute("accept", candidate_profile="oidn_native_v1")
        with self.assertRaisesRegex(RuntimeError, "benchmark capability"):
            self.execute(
                "accept",
                candidate_profile="oidn_native_v1",
                candidate_profile_scope="benchmark_screen_v1",
            )

        output = self.execute("accept", **profile_environment)
        manifest_path, manifest = self.manifest_for(output)
        expected_oidn = {
            "use_denoising": True,
            "view_layer_use_denoising": True,
            "denoiser": "OPENIMAGEDENOISE",
            "denoising_input_passes": "RGB_ALBEDO_NORMAL",
            "denoising_prefilter": "ACCURATE",
            "denoising_quality": "HIGH",
            "denoising_use_gpu": False,
        }
        expected_off = {
            "use_denoising": False,
            "view_layer_use_denoising": False,
            "denoiser": None,
            "denoising_input_passes": None,
            "denoising_prefilter": None,
            "denoising_quality": None,
            "denoising_use_gpu": None,
        }
        self.assertEqual(
            manifest["render"]["integrator_policy"]["actual_integrator"],
            {
                "max_bounces": 32,
                "diffuse_bounces": 16,
                "glossy_bounces": 16,
                "transmission_bounces": 32,
            },
        )
        self.assertEqual(
            manifest["render"]["integrator_policy"]["mode"],
            "fixed_reference",
        )
        self.assertEqual(
            manifest["render"]["integrator_policy"]["actual_sampling"],
            {
                "use_light_tree": False,
                "use_adaptive_sampling": False,
                "adaptive_min_samples": 0,
                "adaptive_threshold": 0.01,
            },
        )
        self.assertEqual(
            manifest["render"]["denoising_policy"],
            {
                "mode": "candidate_oidn",
                "candidate_policy": expected_oidn,
                "actual": expected_oidn,
                "repair_and_baseline_denoising_disabled": True,
            },
        )
        renderer = manifest["render"]["worker_renderer_identity"]
        self.assertEqual(renderer["candidate_denoising_policy"], expected_oidn)
        self.assertEqual(renderer["reference_denoising_policy"], expected_off)
        configs = [
            json.loads(path.read_text())
            for path in manifest_path.parent.glob("*-render-config.json")
        ]
        self.assertTrue(configs)
        self.assertTrue(
            all(row["candidate_denoising_policy"] == expected_oidn for row in configs)
        )

        repaired = self.execute("repair", **profile_environment)
        _repair_path, repair_manifest = self.manifest_for(repaired)
        self.assertEqual(
            repair_manifest["render"]["denoising_policy"]["mode"],
            "fixed_off_reference",
        )
        self.assertEqual(
            repair_manifest["render"]["denoising_policy"]["actual"],
            expected_off,
        )

        environment = self.environment("accept")
        environment.update(
            {
                "CX_SPEC_RENDER_CYCLES_CANDIDATE_PROFILE": "oidn_native_v1",
                "CX_SPEC_RENDER_CYCLES_CANDIDATE_PROFILE_SCOPE": (
                    "benchmark_screen_v1"
                ),
                "CX_SPEC_RENDER_CYCLES_CANDIDATE_PROFILE_AUTH": benchmark_auth,
            }
        )
        module = self.load_backend(environment)
        unit = module.SpecUnit(
            "local-metal-benchmark",
            "render",
            make_payload(self.scene),
            {"cx_benchmark_profile_auth_v1": benchmark_auth},
        )
        with mock.patch.dict(os.environ, environment, clear=False):
            proposal = module.draft(unit)
            module.baseline(unit)
        context = module._CONTEXTS[proposal.meta["cx_cycles_context"]]
        baseline_manifest = json.loads(
            (context["unit_dir"] / "baseline-manifest.json").read_text()
        )
        self.assertEqual(
            baseline_manifest["render"]["denoising_policy"]["actual"],
            expected_off,
        )

        source = module._BLENDER_CHILD_SOURCE
        for property_name in (
            "OPENIMAGEDENOISE",
            "RGB_ALBEDO_NORMAL",
            "ACCURATE",
            "HIGH",
            "denoising_use_gpu",
        ):
            self.assertIn(property_name, source)

    def test_oidn_denoising_attestation_mismatch_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "handshake shape"):
            self.execute(
                "missing_denoising_attestation",
                candidate_profile="oidn_native_v1",
                candidate_profile_scope="benchmark_screen_v1",
                candidate_profile_auth="e" * 64,
            )
        with self.assertRaisesRegex(RuntimeError, "denoising profile"):
            self.execute(
                "wrong_denoising",
                candidate_profile="oidn_native_v1",
                candidate_profile_scope="benchmark_screen_v1",
                candidate_profile_auth="e" * 64,
            )

    def test_candidate_profile_scope_and_capability_are_session_snapshot_pins(self):
        environment = self.environment("accept")
        environment.update(
            {
                "CX_SPEC_RENDER_CYCLES_CANDIDATE_PROFILE": "cap8_v1",
                "CX_SPEC_RENDER_CYCLES_CANDIDATE_PROFILE_SCOPE": (
                    "benchmark_screen_v1"
                ),
                "CX_SPEC_RENDER_CYCLES_CANDIDATE_PROFILE_AUTH": "b" * 64,
            }
        )
        module = self.load_backend(environment)
        unit = module.SpecUnit(
            "local-metal-benchmark",
            "render",
            make_payload(self.scene),
            {"cx_benchmark_profile_auth_v1": "b" * 64},
        )
        with mock.patch.dict(os.environ, environment, clear=False):
            module._context_for_unit(unit)
            os.environ[
                "CX_SPEC_RENDER_CYCLES_CANDIDATE_PROFILE_SCOPE"
            ] = "native_only"
            with self.assertRaisesRegex(
                module.CyclesPreviewError, "configuration changed"
            ):
                module._context_for_unit(unit)

    def test_divergent_independent_drafts_trigger_high_spp_repair(self):
        output = self.execute("repair")
        manifest_path, manifest = self.manifest_for(output)

        self.assertEqual(manifest_path.name, "repair-manifest.json")
        self.assertEqual(manifest["phase"], "repair")
        self.assertEqual(manifest["render"]["samples"], 16)
        self.assertEqual(self.rendered_phases(), {"draft", "verify", "repair"})
        self.assertEqual(output["receipt"]["accepted_units"], 0)
        self.assertEqual(output["receipt"]["repaired_units"], 1)
        verification = json.loads(
            (manifest_path.parent / "verification-manifest.json").read_text()
        )
        self.assertFalse(verification["accepted"])
        # RGB is maximally divergent while the opaque alpha channel agrees.
        self.assertAlmostEqual(verification["global_agreement"], 0.0)

    def test_verifier_and_baseline_use_nonoverlapping_sample_ranges(self):
        output = self.execute(
            "accept", draft_samples=8, verify_samples=4, repair_samples=16
        )
        manifest_path, _manifest = self.manifest_for(output)
        configs = {
            json.loads(path.read_text())["phase"]: json.loads(path.read_text())
            for path in manifest_path.parent.glob("*-render-config.json")
        }
        self.assertEqual(configs["draft"]["sample_offset"], 0)
        self.assertEqual(configs["verify"]["sample_offset"], 8)

        environment = self.environment("accept")
        module = self.load_backend(environment)
        unit = module.SpecUnit(
            "baseline-range",
            "render",
            make_payload(
                self.scene, draft_samples=8, verify_samples=4, repair_samples=16
            ),
            {},
        )
        with mock.patch.dict(os.environ, environment, clear=False):
            descriptor = module.baseline(unit)
        context = module._CONTEXTS[next(iter(module._CONTEXTS))]
        baseline_config = json.loads(
            (context["unit_dir"] / "baseline-render-config.json").read_text()
        )
        self.assertEqual(baseline_config["sample_offset"], 12)
        baseline_manifest = json.loads(
            (
                self.output_root / descriptor["manifest_path"]
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(baseline_manifest["render"]["sample_offset"], 12)

    def test_localized_failure_uses_coalesced_selective_repair(self):
        output = self.execute("selective", width=128, height=128)
        manifest_path, manifest = self.manifest_for(output)

        self.assertEqual(manifest["phase"], "repair")
        self.assertEqual(manifest["artifact"]["path"].split("/")[-1], "repair-selective.png")
        self.assertEqual(
            manifest["render"]["pixel_filter"],
            {"type": "BLACKMAN_HARRIS", "width": 1.5},
        )
        self.assertFalse(manifest["artifact_verified"])
        self.assertEqual(manifest["evidence"], "synthetic")
        strategy = manifest["repair_strategy"]
        self.assertEqual(strategy["attempted"], "selective")
        self.assertEqual(strategy["disposition"], "selective_composite")
        self.assertFalse(strategy["full_frame_escalated"])
        self.assertEqual(strategy["initial_failing_tile_count"], 1)
        self.assertEqual(strategy["rectangles"], [[28, 28, 68, 68]])
        self.assertEqual(len(strategy["patches"]), 1)
        self.assertEqual(strategy["patches"][0]["rect"], [28, 28, 68, 68])
        self.assertTrue(strategy["post_patch_agreement"]["independent_full_frame"])
        self.assertTrue(strategy["post_patch_agreement"]["passed"])
        self.assertFalse(strategy["artifact_verified"])

        verification = json.loads(
            (manifest_path.parent / "verification-manifest.json").read_text()
        )
        self.assertEqual(verification["tile_count"], 16)
        self.assertEqual(
            verification["tile_grid"],
            {
                "columns": 4,
                "rows": 4,
                "max_long_edge_tiles": 16,
                "minimum_nominal_edge_pixels": 32,
            },
        )
        self.assertEqual(verification["failing_tile_count"], 1)
        self.assertEqual(verification["failing_tiles"][0]["rect"], [32, 32, 64, 64])
        self.assertFalse(verification["failing_tiles_truncated"])
        self.assertEqual(verification["repair_plan"]["mode"], "selective")

        delivered = (self.output_root / manifest["artifact"]["path"]).resolve()
        verifier = manifest_path.parent / "verify.png"
        with Image.open(delivered) as actual, Image.open(verifier) as expected:
            self.assertIsNone(
                ImageChops.difference(
                    actual.convert("RGBA"), expected.convert("RGBA")
                ).getbbox()
            )

    def test_agreement_grid_is_resolution_normalized_and_balanced(self):
        module = self.load_backend(self.environment("accept"))
        self.assertEqual(module._agreement_grid((512, 288)), (16, 9))
        self.assertEqual(module._agreement_grid((1920, 1080)), (16, 9))
        self.assertEqual(module._agreement_grid((128, 128)), (4, 4))
        self.assertEqual(module._agreement_grid((16, 4096)), (1, 16))
        for edge, expected in (
            (33, 1),
            (63, 1),
            (65, 2),
            (129, 4),
            (511, 15),
        ):
            with self.subTest(edge=edge):
                self.assertEqual(module._agreement_grid((edge, edge)), (expected, expected))
        self.assertEqual(module._microtile_grid((1920, 1080)), (60, 33))
        self.assertEqual(module._microtile_grid((33, 63)), (1, 1))

    def test_fixed_microtile_gate_catches_small_defect_diluted_by_regional_grid(self):
        environment = self.environment("accept")
        module = self.load_backend(environment)
        clean = self.root / "clean-1080.png"
        defect = self.root / "defect-1080.png"
        Image.new("RGB", (1920, 1080), (0, 0, 0)).save(clean)
        changed = Image.new("RGB", (1920, 1080), (0, 0, 0))
        changed.paste((255, 255, 255), (0, 0, 32, 32))
        changed.save(defect)
        global_score, regional_worst, _, micro_worst, micro_count = module._agreement(
            clean, defect, (1920, 1080)
        )
        self.assertGreater(global_score, module.GLOBAL_AGREEMENT_MIN)
        self.assertGreater(regional_worst, module.WORST_TILE_AGREEMENT_MIN)
        self.assertLess(micro_worst, module.MICROTILE_AGREEMENT_MIN)
        self.assertEqual(micro_count, 60 * 33)

    def test_selective_repair_miss_escalates_to_full_frame(self):
        output = self.execute("selective_escalate", width=128, height=128)
        manifest_path, manifest = self.manifest_for(output)

        self.assertEqual(manifest["artifact"]["path"].split("/")[-1], "repair.png")
        strategy = manifest["repair_strategy"]
        self.assertEqual(strategy["attempted"], "selective")
        self.assertEqual(strategy["disposition"], "full_frame")
        self.assertTrue(strategy["full_frame_escalated"])
        self.assertEqual(strategy["reason"], "post_patch_full_frame_agreement_failed")
        self.assertFalse(strategy["post_patch_agreement"]["passed"])
        self.assertEqual(len(strategy["patches"]), 1)
        config_names = {
            path.name for path in manifest_path.parent.glob("*-render-config.json")
        }
        self.assertIn("repair-patch-00-render-config.json", config_names)
        self.assertIn("repair-full-render-config.json", config_names)

        delivered = (self.output_root / manifest["artifact"]["path"]).resolve()
        verifier = manifest_path.parent / "verify.png"
        with Image.open(delivered) as actual, Image.open(verifier) as expected:
            self.assertIsNone(
                ImageChops.difference(
                    actual.convert("RGBA"), expected.convert("RGBA")
                ).getbbox()
            )

    def test_repair_plan_coalesces_halo_and_has_hard_cutoffs(self):
        environment = self.environment()
        module = self.load_backend(environment)

        adjacent = [
            {"rect": (32, 32, 64, 64), "score": 0.1},
            {"rect": (64, 32, 96, 64), "score": 0.2},
        ]
        plan = module._select_repair_plan(
            0.95, adjacent, width=256, height=256
        )
        self.assertEqual(plan["mode"], "selective")
        self.assertEqual(plan["rectangles"], [(28, 28, 100, 68)])

        disconnected = [
            {"rect": (5 + 25 * index, 5, 6 + 25 * index, 6), "score": 0.1}
            for index in range(module.MAX_SELECTIVE_COMPONENTS + 1)
        ]
        component_plan = module._select_repair_plan(
            0.95, disconnected, width=256, height=64
        )
        self.assertEqual(component_plan["mode"], "full_frame")
        self.assertEqual(component_plan["reason"], "component_limit_exceeded")

        dense = [
            {"rect": (0, 0, 32, 32), "score": 0.1},
            {"rect": (32, 0, 64, 32), "score": 0.1},
            {"rect": (0, 32, 32, 64), "score": 0.1},
            {"rect": (32, 32, 64, 64), "score": 0.1},
        ]
        area_plan = module._select_repair_plan(
            0.95, dense, width=128, height=128
        )
        self.assertEqual(area_plan["mode"], "full_frame")
        self.assertEqual(area_plan["reason"], "repair_area_limit_exceeded")

        too_many = [
            {"rect": (0, 0, 1, 1), "score": 0.1}
            for _ in range(module.MAX_SELECTIVE_FAILING_TILES + 1)
        ]
        count_plan = module._select_repair_plan(
            0.95, too_many, width=128, height=128
        )
        self.assertEqual(count_plan["mode"], "full_frame")
        self.assertEqual(count_plan["reason"], "failing_tile_limit_exceeded")

    def test_renderer_failure_fails_closed_to_lazy_high_spp_baseline(self):
        output = self.execute("fail_verify")
        manifest_path, manifest = self.manifest_for(output)

        self.assertEqual(manifest_path.name, "baseline-manifest.json")
        self.assertEqual(manifest["phase"], "baseline")
        self.assertEqual(manifest["render"]["samples"], 16)
        self.assertEqual(manifest["render"]["sample_offset"], 8)
        self.assertEqual(self.rendered_phases(), {"draft", "verify", "baseline"})
        self.assertEqual(output["receipt"]["quality_tier"], "fail")
        self.assertFalse(output["receipt"]["quality_gate"])
        self.assertEqual(output["receipt"]["baseline_source"], "absent")
        self.assertIsNone(output["receipt"]["speedup_vs_baseline"])

    def test_accept_and_repair_reuse_one_resident_blender_spawn(self):
        for mode, expected_commands in (("accept", 2), ("repair", 3)):
            with self.subTest(mode=mode):
                environment = self.environment(mode)
                module = self.load_backend(environment)
                unit = module.SpecUnit(
                    f"resident-{mode}", "render", make_payload(self.scene), {}
                )
                real_popen = module.subprocess.Popen
                processes: list[subprocess.Popen] = []
                observed_kwargs: list[dict[str, object]] = []

                def observed_popen(*args, **kwargs):
                    observed_kwargs.append(dict(kwargs))
                    process = real_popen(*args, **kwargs)
                    processes.append(process)
                    return process

                with (
                    mock.patch.dict(os.environ, environment, clear=False),
                    mock.patch.object(
                        module.subprocess, "Popen", side_effect=observed_popen
                    ),
                ):
                    proposal = module.draft(unit)
                    verification = module.verify(proposal)
                    if not verification.accepted:
                        module.repair(proposal, verification)

                self.assertEqual(len(processes), 1)
                self.assertEqual(module._WORKER["commands"], expected_commands)
                self.assertEqual(len(observed_kwargs[0]["pass_fds"]), 2)
                self.assertNotIn("start_new_session", observed_kwargs[0])
                self.assertNotIn("preexec_fn", observed_kwargs[0])
                module._shutdown_worker()
                self.assertIsNotNone(processes[0].poll())

    def test_local_benchmark_can_own_a_renderer_process_group(self):
        environment = self.environment("accept")
        environment["CX_SPEC_RENDER_CYCLES_LOCAL_PROCESS_GROUP"] = "1"
        module = self.load_backend(environment)
        unit = module.SpecUnit("local-group", "render", make_payload(self.scene), {})
        real_popen = module.subprocess.Popen
        observed_kwargs: list[dict[str, object]] = []

        def observed_popen(*args, **kwargs):
            observed_kwargs.append(dict(kwargs))
            return real_popen(*args, **kwargs)

        with (
            mock.patch.dict(os.environ, environment, clear=False),
            mock.patch.object(module.subprocess, "Popen", side_effect=observed_popen),
        ):
            module.draft(unit)
        self.assertTrue(observed_kwargs[0]["start_new_session"])
        self.assertTrue(module._WORKER["owns_process_group"])

    def test_worker_protocol_faults_quarantine_the_renderer(self):
        cases = (
            ("bad_handshake", "handshake identity"),
            ("bad_device", "handshake identity"),
            ("malformed_response", "malformed strict JSON"),
            ("oversized_response", "fixed bound"),
            ("stale_id", "response identity"),
            ("digest_mismatch", "response identity"),
            ("wrong_integrator", "integrator profile"),
            ("wrong_sampling", "sampling profile"),
            ("early_exit", "exited"),
        )
        for mode, expected in cases:
            with self.subTest(mode=mode):
                environment = self.environment(mode)
                environment["CX_SPEC_RENDER_CYCLES_TIMEOUT_SECS"] = "10"
                module = self.load_backend(environment)
                unit = module.SpecUnit(
                    f"protocol-{mode}", "render", make_payload(self.scene), {}
                )
                real_popen = module.subprocess.Popen
                processes: list[subprocess.Popen] = []

                def observed_popen(*args, **kwargs):
                    process = real_popen(*args, **kwargs)
                    processes.append(process)
                    return process

                with (
                    mock.patch.dict(os.environ, environment, clear=False),
                    mock.patch.object(
                        module.subprocess, "Popen", side_effect=observed_popen
                    ),
                    self.assertRaisesRegex(module.CyclesPreviewError, expected),
                ):
                    module.draft(unit)

                self.assertIsNone(module._WORKER)
                self.assertEqual(len(processes), 1)
                self.assertIsNotNone(processes[0].poll())

    def test_resident_worker_has_a_bounded_command_lifetime(self):
        environment = self.environment("repair")
        module = self.load_backend(environment)
        unit = module.SpecUnit(
            "bounded-resident", "render", make_payload(self.scene), {}
        )
        real_popen = module.subprocess.Popen
        processes: list[subprocess.Popen] = []

        def observed_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process

        with (
            mock.patch.dict(os.environ, environment, clear=False),
            mock.patch.object(module, "MAX_WORKER_COMMANDS", 2),
            mock.patch.object(module.subprocess, "Popen", side_effect=observed_popen),
        ):
            proposal = module.draft(unit)
            verification = module.verify(proposal)
            self.assertFalse(verification.accepted)
            module.repair(proposal, verification)

        self.assertEqual(len(processes), 2)
        self.assertIsNotNone(processes[0].poll())
        self.assertEqual(module._WORKER["commands"], 1)

    def test_payload_rejects_execution_fields_paths_dimensions_pixels_and_samples(self):
        module = self.load_backend(self.environment())
        good = make_payload(self.scene)
        invalid: list[tuple[str, dict[str, object]]] = [
            ("exactly", {**good, "command": ["touch", "/tmp/pwned"]}),
            ("relative", {**good, "scene_path": "/tmp/evil.blend"}),
            ("parent", {**good, "scene_path": "../tiny.blend"}),
            ("dot", {**good, "scene_path": "./tiny.blend"}),
            ("POSIX", {**good, "scene_path": "a\\tiny.blend"}),
            ("lowercase", {**good, "scene_path": "tiny.BLEND"}),
            ("width", {**good, "width": True}),
            ("width", {**good, "width": 4097}),
            ("pixel", {**good, "width": 4096, "height": 4096}),
            ("draft_samples", {**good, "draft_samples": 65}),
            ("repair_samples", {**good, "repair_samples": 4}),
            ("scene_sha256", {**good, "scene_sha256": "A" * 64}),
        ]
        for expected, payload in invalid:
            with self.subTest(expected=expected, payload=payload):
                unit = module.SpecUnit("u", "render", payload, {})
                with self.assertRaisesRegex(module.CyclesPreviewError, expected):
                    module._parse_payload(unit)

    def test_operator_pins_roots_timeout_scene_hash_and_symlink_are_enforced(self):
        good_env = self.environment()

        bad_sha_env = dict(good_env)
        bad_sha_env["CX_SPEC_RENDER_CYCLES_BLENDER_SHA256"] = "0" * 64
        module = self.load_backend(bad_sha_env)
        unit = module.SpecUnit("u", "render", make_payload(self.scene), {})
        with mock.patch.dict(os.environ, bad_sha_env, clear=False):
            with self.assertRaisesRegex(module.CyclesPreviewError, "mismatch"):
                module.draft(unit)

        bad_timeout_env = dict(good_env)
        bad_timeout_env["CX_SPEC_RENDER_CYCLES_TIMEOUT_SECS"] = "0"
        module = self.load_backend(bad_timeout_env)
        unit = module.SpecUnit("u", "render", make_payload(self.scene), {})
        with mock.patch.dict(os.environ, bad_timeout_env, clear=False):
            with self.assertRaisesRegex(module.CyclesPreviewError, "must be in"):
                module.draft(unit)

        bad_device_env = dict(good_env)
        bad_device_env["CX_SPEC_RENDER_CYCLES_DEVICE"] = "CUDA"
        module = self.load_backend(bad_device_env)
        unit = module.SpecUnit("u", "render", make_payload(self.scene), {})
        with mock.patch.dict(os.environ, bad_device_env, clear=False):
            with self.assertRaisesRegex(module.CyclesPreviewError, "must be one of"):
                module.draft(unit)

        metal_env = dict(good_env)
        metal_env["CX_SPEC_RENDER_CYCLES_DEVICE"] = "metal"
        module = self.load_backend(metal_env)
        unit = module.SpecUnit("metal", "render", make_payload(self.scene), {})
        with mock.patch.dict(os.environ, metal_env, clear=False):
            proposal = module.draft(unit)
        context = module._CONTEXTS[proposal.meta["cx_cycles_context"]]
        manifest = json.loads(
            (context["unit_dir"] / "draft-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["render"]["device"], "GPU/METAL")
        self.assertEqual(module._WORKER["key"][-1], "METAL")

        wrong_scene = make_payload(self.scene, scene_sha256="f" * 64)
        module = self.load_backend(good_env)
        unit = module.SpecUnit("u", "render", wrong_scene, {})
        with mock.patch.dict(os.environ, good_env, clear=False):
            with self.assertRaisesRegex(module.CyclesPreviewError, "scene_sha256 mismatch"):
                module.draft(unit)

        outside = self.root / "outside.blend"
        outside.write_bytes(b"outside")
        link = self.scene_root / "linked.blend"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        linked = make_payload(link)
        module = self.load_backend(good_env)
        unit = module.SpecUnit("u", "render", linked, {})
        with mock.patch.dict(os.environ, good_env, clear=False):
            with self.assertRaisesRegex(module.CyclesPreviewError, "symlink"):
                module.draft(unit)

    def test_png_shape_format_timeout_and_proposal_binding_fail_closed(self):
        for mode, expected in (
            ("invalid_png", "decode"),
            ("wrong_size", "expected"),
        ):
            with self.subTest(mode=mode):
                environment = self.environment(mode)
                module = self.load_backend(environment)
                unit = module.SpecUnit("u", "render", make_payload(self.scene), {})
                with mock.patch.dict(os.environ, environment, clear=False):
                    with self.assertRaisesRegex(module.CyclesPreviewError, expected):
                        module.draft(unit)

        environment = self.environment("sleep")
        environment["CX_SPEC_RENDER_CYCLES_TIMEOUT_SECS"] = "1"
        module = self.load_backend(environment)
        unit = module.SpecUnit("u", "render", make_payload(self.scene), {})
        with mock.patch.dict(os.environ, environment, clear=False):
            with self.assertRaisesRegex(module.CyclesPreviewError, "exceeded 1 seconds"):
                module.draft(unit)

        environment = self.environment("accept")
        module = self.load_backend(environment)
        unit = module.SpecUnit("bound", "render", make_payload(self.scene), {})
        with mock.patch.dict(os.environ, environment, clear=False):
            real_popen = module.subprocess.Popen
            observed_kwargs: list[dict[str, object]] = []

            def inherited_group_popen(*args, **kwargs):
                observed_kwargs.append(dict(kwargs))
                return real_popen(*args, **kwargs)

            with mock.patch.object(
                module.subprocess,
                "Popen",
                side_effect=inherited_group_popen,
            ):
                proposal = module.draft(unit)
            self.assertEqual(len(observed_kwargs), 1)
            self.assertNotIn("start_new_session", observed_kwargs[0])
            self.assertNotIn("preexec_fn", observed_kwargs[0])
            forged = module.DraftProposal(unit, {"modified": True}, proposal.meta)
            with self.assertRaisesRegex(module.CyclesPreviewError, "modified"):
                module.verify(forged)
            context = module._CONTEXTS[proposal.meta["cx_cycles_context"]]
            context["scene_copy"].write_bytes(b"tampered after the draft")
            with self.assertRaisesRegex(module.CyclesPreviewError, "scene copy changed"):
                module.verify(proposal)

    def test_png_validation_uses_one_no_follow_snapshot_and_rejects_races(self):
        module = self.load_backend(self.environment("accept"))
        artifact = self.root / "snapshot.png"
        Image.new("RGBA", (16, 16), (11, 22, 33, 44)).save(artifact)
        original_bytes = artifact.read_bytes()
        real_open = module.os.open
        real_image_open = module.Image.open
        opened: list[tuple[Path, int]] = []
        decoder_inputs: list[object] = []

        def observed_open(path, flags, *args):
            opened.append((Path(path), flags))
            return real_open(path, flags, *args)

        def observed_image_open(source, *args, **kwargs):
            decoder_inputs.append(source)
            return real_image_open(source, *args, **kwargs)

        with (
            mock.patch.object(module.os, "open", side_effect=observed_open),
            mock.patch.object(module.Image, "open", side_effect=observed_image_open),
            mock.patch.object(
                module,
                "_sha256_file",
                side_effect=AssertionError("PNG validation reopened for hashing"),
            ),
        ):
            actual_sha = module._validate_png(artifact, width=16, height=16)
        self.assertEqual(actual_sha, hashlib.sha256(original_bytes).hexdigest())
        self.assertEqual(len(opened), 1)
        if hasattr(module.os, "O_NOFOLLOW"):
            self.assertTrue(opened[0][1] & module.os.O_NOFOLLOW)
        self.assertEqual(len(decoder_inputs), 1)
        self.assertIsInstance(decoder_inputs[0], io.BytesIO)

        replacement = self.root / "replacement.png"
        Image.new("RGBA", (16, 16), (99, 88, 77, 66)).save(replacement)
        real_read = module.os.read
        replaced = False

        def racing_read(fd, count):
            nonlocal replaced
            chunk = real_read(fd, count)
            if not chunk and not replaced:
                os.replace(replacement, artifact)
                replaced = True
            return chunk

        with mock.patch.object(module.os, "read", side_effect=racing_read):
            with self.assertRaisesRegex(module.CyclesPreviewError, "changed"):
                module._validate_png(artifact, width=16, height=16)
        self.assertTrue(replaced)

        target = self.root / "target.png"
        Image.new("RGB", (16, 16), (1, 2, 3)).save(target)
        link = self.root / "linked.png"
        try:
            link.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaisesRegex(module.CyclesPreviewError, "non-symlink"):
            module._validate_png(link, width=16, height=16)

        wrong_shape = self.root / "wrong-shape.png"
        Image.new("RGB", (17, 16), (1, 2, 3)).save(wrong_shape)
        with self.assertRaisesRegex(module.CyclesPreviewError, "expected"):
            module._validate_png(wrong_shape, width=16, height=16)

        palette = self.root / "palette.png"
        Image.new("P", (16, 16), 1).save(palette)
        with self.assertRaisesRegex(module.CyclesPreviewError, "RGB or RGBA"):
            module._validate_png(palette, width=16, height=16)

    def test_retained_png_capture_and_one_shot_bound_pop(self):
        environment = self.environment("accept")
        module = self.load_backend(environment)
        unit = module.SpecUnit("retained", "render", make_payload(self.scene), {})
        with mock.patch.dict(os.environ, environment, clear=False):
            context = module._context_for_unit(unit)
            output = context["unit_dir"] / "retained-draft.png"
            artifact_sha = module._invoke_blender(
                context,
                "draft",
                4,
                context["seeds"]["draft"],
                output,
                execution_label="retained-draft",
                retain_validated_png=True,
            )

        cache = context[module._RETAINED_VALIDATED_PNGS_KEY]
        self.assertEqual(set(cache), {"draft"})
        record = cache["draft"]
        self.assertIsInstance(record, module.RetainedValidatedPng)
        self.assertEqual(record.path, output.absolute())
        self.assertEqual(record.phase, "draft")
        self.assertEqual(record.context_binding_sha256, context["binding"])
        self.assertEqual(record.sha256, artifact_sha)
        self.assertEqual(hashlib.sha256(record.source_bytes).hexdigest(), artifact_sha)
        self.assertEqual(record.mode, "RGB")
        self.assertEqual(len(record.pixel_bytes), 64 * 64 * 3)
        self.assertEqual(
            hashlib.sha256(record.pixel_bytes).hexdigest(), record.pixel_sha256
        )
        with self.assertRaises(AttributeError):
            record.phase = "verify"

        consumed = module._pop_retained_validated_png(
            context, phase="draft", path=output, sha256=artifact_sha
        )
        self.assertIs(consumed, record)
        self.assertNotIn(module._RETAINED_VALIDATED_PNGS_KEY, context)
        with self.assertRaisesRegex(module.CyclesPreviewError, "unavailable"):
            module._pop_retained_validated_png(
                context, phase="draft", path=output, sha256=artifact_sha
            )

    def test_retained_png_substitution_and_replacement_fail_closed(self):
        environment = self.environment("accept")
        module = self.load_backend(environment)
        first_unit = module.SpecUnit(
            "retained-first", "render", make_payload(self.scene), {}
        )
        second_unit = module.SpecUnit(
            "retained-second", "render", make_payload(self.scene, frame=2), {}
        )
        with mock.patch.dict(os.environ, environment, clear=False):
            first = module._context_for_unit(first_unit)
            second = module._context_for_unit(second_unit)
            output = first["unit_dir"] / "retained-first.png"
            artifact_sha = module._invoke_blender(
                first,
                "draft",
                4,
                first["seeds"]["draft"],
                output,
                execution_label="retained-first",
                retain_validated_png=True,
            )
        record = first[module._RETAINED_VALIDATED_PNGS_KEY]["draft"]
        second[module._RETAINED_VALIDATED_PNGS_KEY] = {"draft": record}
        with self.assertRaisesRegex(module.CyclesPreviewError, "identity mismatch"):
            module._pop_retained_validated_png(
                second, phase="draft", path=output, sha256=artifact_sha
            )
        self.assertNotIn(module._RETAINED_VALIDATED_PNGS_KEY, second)

        snapshot = module._validated_png_snapshot(
            output, width=64, height=64, retain_pixels=True
        )
        module._discard_retained_validated_pngs(first)
        module._cache_retained_validated_png(first, "draft", snapshot)
        module._cache_retained_validated_png(first, "verify", snapshot)
        self.assertEqual(
            set(first[module._RETAINED_VALIDATED_PNGS_KEY]),
            {"draft", "verify"},
        )
        with self.assertRaisesRegex(module.CyclesPreviewError, "replacement"):
            module._cache_retained_validated_png(first, "draft", snapshot)
        self.assertNotIn(module._RETAINED_VALIDATED_PNGS_KEY, first)

        module._cache_retained_validated_png(first, "draft", snapshot)
        with self.assertRaisesRegex(module.CyclesPreviewError, "identity mismatch"):
            module._pop_retained_validated_png(
                first, phase="draft", path=output, sha256="0" * 64
            )
        self.assertNotIn(module._RETAINED_VALIDATED_PNGS_KEY, first)

    def test_failed_retained_verify_discards_the_draft_capability(self):
        environment = self.environment("fail_verify")
        module = self.load_backend(environment)
        unit = module.SpecUnit(
            "retained-cleanup", "render", make_payload(self.scene), {}
        )
        with mock.patch.dict(os.environ, environment, clear=False):
            context = module._context_for_unit(unit)
            draft_output = context["unit_dir"] / "retained-cleanup-draft.png"
            module._invoke_blender(
                context,
                "draft",
                4,
                context["seeds"]["draft"],
                draft_output,
                execution_label="retained-cleanup-draft",
                retain_validated_png=True,
            )
            self.assertIn(module._RETAINED_VALIDATED_PNGS_KEY, context)
            verify_output = context["unit_dir"] / "retained-cleanup-verify.png"
            with self.assertRaises(module.CyclesPreviewError):
                module._invoke_blender(
                    context,
                    "verify",
                    4,
                    context["seeds"]["verify"],
                    verify_output,
                    execution_label="retained-cleanup-verify",
                    retain_validated_png=True,
                )
        self.assertNotIn(module._RETAINED_VALIDATED_PNGS_KEY, context)
        module._discard_retained_validated_pngs(context)
        self.assertNotIn(module._RETAINED_VALIDATED_PNGS_KEY, context)

    def test_private_scene_dependency_bundle_is_pinned_and_revalidated(self):
        dependency = self.scene_root / "texture.bin"
        dependency.write_bytes(b"operator-pinned texture bytes")
        environment = self.environment("accept")
        module = self.load_backend(environment)
        unit = module.SpecUnit("bundle", "render", make_payload(self.scene), {})
        with mock.patch.dict(os.environ, environment, clear=False):
            proposal = module.draft(unit)
            context = module._CONTEXTS[proposal.meta["cx_cycles_context"]]
            copied_dependency = context["scene_bundle"]["root"] / "texture.bin"
            self.assertEqual(copied_dependency.read_bytes(), dependency.read_bytes())
            copied_dependency.write_bytes(b"tampered")
            with self.assertRaisesRegex(module.CyclesPreviewError, "bundle changed"):
                module.verify(proposal)

    def test_render_hot_path_uses_stat_sentinels_not_repeat_bundle_hashes(self):
        dependency = self.scene_root / "texture.bin"
        dependency.write_bytes(b"operator-pinned texture bytes")
        environment = self.environment("accept")
        module = self.load_backend(environment)
        unit = module.SpecUnit("stat-hot-path", "render", make_payload(self.scene), {})
        with mock.patch.dict(os.environ, environment, clear=False):
            context = module._context_for_unit(unit)
            original_hash = module._sha256_file
            original_snapshot = module._read_png_snapshot
            hashed_paths: list[Path] = []
            snapshot_paths: list[Path] = []

            def observed_hash(path, **kwargs):
                hashed_paths.append(Path(path))
                return original_hash(path, **kwargs)

            def observed_snapshot(path, **kwargs):
                snapshot_paths.append(Path(path))
                return original_snapshot(path, **kwargs)

            with (
                mock.patch.object(module, "_sha256_file", side_effect=observed_hash),
                mock.patch.object(
                    module, "_read_png_snapshot", side_effect=observed_snapshot
                ),
            ):
                proposal = module.draft(unit)
                module.verify(proposal)

        self.assertTrue(
            {"draft.png", "verify.png"}
            <= {path.name for path in snapshot_paths}
        )
        self.assertFalse(any(path.name == "draft.png" for path in hashed_paths))
        self.assertNotIn(context["session"]["blender"], hashed_paths)
        self.assertFalse(
            any(
                context["scene_bundle"]["root"] in path.parents
                for path in hashed_paths
            )
        )

    def test_reused_worker_identity_is_propagated_to_each_frame_manifest(self):
        environment = self.environment("accept")
        module = self.load_backend(environment)
        first = module.SpecUnit("frame-1", "render", make_payload(self.scene, frame=1), {})
        second = module.SpecUnit("frame-2", "render", make_payload(self.scene, frame=2), {})
        real_popen = module.subprocess.Popen
        processes: list[subprocess.Popen] = []

        def observed_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process

        with (
            mock.patch.dict(os.environ, environment, clear=False),
            mock.patch.object(module.subprocess, "Popen", side_effect=observed_popen),
        ):
            proposals = [module.draft(first), module.draft(second)]

        identities = []
        for proposal in proposals:
            context = module._CONTEXTS[proposal.meta["cx_cycles_context"]]
            manifest = json.loads(
                (context["unit_dir"] / "draft-manifest.json").read_text()
            )
            identities.append(manifest["render"]["worker_renderer_identity"])
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(identities[0])
        self.assertEqual(identities[0], identities[1])


_REAL_SMOKE = os.environ.get("CX_RUN_REAL_CYCLES_PREVIEW_SMOKE") == "1"


@unittest.skipUnless(
    _REAL_SMOKE,
    "set CX_RUN_REAL_CYCLES_PREVIEW_SMOKE=1 for the installed Blender smoke",
)
class RealCyclesPreviewSmoke(unittest.TestCase):
    def test_real_64x64_cycles_through_pinned_wire_protocol(self):
        blender = Path(os.environ.get(
            "CX_REAL_CYCLES_BLENDER",
            "/Applications/Blender.app/Contents/MacOS/Blender",
        ))
        if not blender.is_file():
            self.skipTest(f"Blender not installed at {blender}")

        with tempfile.TemporaryDirectory() as temp_raw:
            temp = Path(temp_raw)
            scenes = temp / "scenes"
            outputs = temp / "outputs"
            scenes.mkdir()
            outputs.mkdir()
            scene = scenes / "tiny.blend"
            create_script = temp / "create_tiny_scene.py"
            create_script.write_text(textwrap.dedent(r'''
                from pathlib import Path
                import sys
                import bpy

                target = Path(sys.argv[sys.argv.index("--") + 1])
                bpy.ops.object.select_all(action="SELECT")
                bpy.ops.object.delete(use_global=False)
                bpy.ops.mesh.primitive_cube_add(location=(0.0, 0.0, 0.6))
                cube = bpy.context.object
                material = bpy.data.materials.new("blue")
                material.diffuse_color = (0.08, 0.25, 0.8, 1.0)
                cube.data.materials.append(material)
                bpy.ops.mesh.primitive_plane_add(size=8.0, location=(0.0, 0.0, 0.0))
                camera_data = bpy.data.cameras.new("Camera")
                camera = bpy.data.objects.new("Camera", camera_data)
                bpy.context.collection.objects.link(camera)
                camera.location = (3.2, -3.2, 2.6)
                camera.rotation_euler = ((cube.location - camera.location)
                                         .to_track_quat("-Z", "Y").to_euler())
                bpy.context.scene.camera = camera
                light_data = bpy.data.lights.new("Key", type="AREA")
                light_data.energy = 700.0
                light_data.shape = "DISK"
                light_data.size = 4.0
                light = bpy.data.objects.new("Key", light_data)
                bpy.context.collection.objects.link(light)
                light.location = (2.0, -2.0, 5.0)
                bpy.context.scene.world.color = (0.03, 0.03, 0.03)
                bpy.ops.wm.save_as_mainfile(filepath=str(target))
            '''), encoding="utf-8")
            created = subprocess.run(
                [
                    str(blender),
                    "--background",
                    "--factory-startup",
                    "--disable-autoexec",
                    "--python",
                    str(create_script),
                    "--",
                    str(scene),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=90,
                check=False,
            )
            self.assertEqual(created.returncode, 0, created.stdout.decode(errors="replace"))
            self.assertTrue(scene.is_file())

            environment = os.environ.copy()
            environment.update({
                driver.BACKEND_ENV: str(BACKEND_PATH),
                driver.BACKEND_SHA_ENV: sha(BACKEND_PATH),
                driver.CORE_SHA_ENV: sha(driver.CORE_PATH),
                driver.ADAPTER_SHA_ENV: sha(driver.ADAPTER_PATH),
                "CX_SPEC_RENDER_CYCLES_BLENDER": str(blender.resolve()),
                "CX_SPEC_RENDER_CYCLES_BLENDER_SHA256": sha(blender),
                "CX_SPEC_RENDER_CYCLES_SCENE_ROOT": str(scenes),
                "CX_SPEC_RENDER_CYCLES_OUTPUT_ROOT": str(outputs),
                "CX_SPEC_RENDER_CYCLES_TIMEOUT_SECS": "120",
                "CX_SPEC_RENDER_CYCLES_DEVICE": os.environ.get(
                    "CX_REAL_CYCLES_DEVICE", "CPU"
                ),
            })
            raw_request = make_request(make_payload(scene))
            executed = subprocess.run(
                [sys.executable, str(DRIVER_PATH)],
                input=raw_request,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                timeout=300,
                check=False,
            )
            self.assertEqual(
                executed.returncode,
                0,
                executed.stderr.decode(errors="replace"),
            )
            result = json.loads(executed.stdout)
            self.assertTrue(result["preview_only"])
            self.assertFalse(result["production_ready"])
            self.assertEqual(result["receipt"]["baseline_source"], "absent")
            self.assertIsNone(result["receipt"]["speedup_vs_baseline"])
            descriptor = result["outputs"][0]
            manifest_path = (outputs / descriptor["manifest_path"]).resolve(strict=True)
            manifest = json.loads(manifest_path.read_text())
            self.assertIn(manifest["phase"], {"draft", "repair"})
            artifact = (outputs / manifest["artifact"]["path"]).resolve(strict=True)
            self.assertTrue(artifact.is_file())

            # Exercise the real Blender border API through the same pinned,
            # resident FD protocol. The output remains full-frame so the
            # parent can crop and composite a deterministic halo rectangle.
            with mock.patch.dict(os.environ, environment, clear=False):
                backend = driver._load_backend(BACKEND_PATH, sha(BACKEND_PATH))
                try:
                    unit = backend.SpecUnit(
                        "real-border-smoke", "render", make_payload(scene), {}
                    )
                    context = backend._context_for_unit(unit)
                    border_output = context["unit_dir"] / "border-smoke.png"
                    border_sha = backend._invoke_blender(
                        context,
                        "repair",
                        4,
                        context["seeds"]["repair"],
                        border_output,
                        border=(8, 8, 40, 40),
                        execution_label="repair-border-smoke",
                    )
                    self.assertEqual(len(border_sha), 64)
                    full_output = context["unit_dir"] / "full-smoke.png"
                    backend._invoke_blender(
                        context,
                        "repair",
                        4,
                        context["seeds"]["repair"],
                        full_output,
                        execution_label="repair-full-smoke",
                    )
                    with (
                        Image.open(border_output) as border_image,
                        Image.open(full_output) as full_image,
                    ):
                        self.assertEqual(border_image.size, (64, 64))
                        rect = (8, 8, 40, 40)
                        self.assertIsNone(
                            ImageChops.difference(
                                border_image.convert("RGBA").crop(rect),
                                full_image.convert("RGBA").crop(rect),
                            ).getbbox()
                        )
                    border_config = json.loads(
                        (context["unit_dir"] / "repair-border-smoke-render-config.json")
                        .read_text(encoding="utf-8")
                    )
                    self.assertEqual(border_config["border"], [8, 8, 40, 40])
                finally:
                    backend._shutdown_worker()

            # Optional outer Rust boundary: same request and backend, with the
            # agent additionally pinning this first-party Python driver.
            if os.environ.get("CX_RUN_RUST_CYCLES_PREVIEW_SMOKE") == "1":
                agent = Path(os.environ.get(
                    "CX_REAL_CX_AGENT",
                    str(HERE.parent.parent / "agent" / "target" / "release" / "cx-agent"),
                ))
                if not agent.is_file():
                    self.skipTest(f"cx-agent not found at {agent}")
                request_path = temp / "request.json"
                request_path.write_bytes(raw_request)
                rust_env = dict(environment)
                rust_env.update({
                    "CX_SPEC_RENDER_PREVIEW_DRIVER": str(DRIVER_PATH),
                    "CX_SPEC_RENDER_PREVIEW_DRIVER_SHA256": sha(DRIVER_PATH),
                    "CX_SPEC_RENDER_PREVIEW_TIMEOUT_SECS": "300",
                })
                rust = subprocess.run(
                    [str(agent), "spec-render-preview", "--input", str(request_path)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=rust_env,
                    timeout=360,
                    check=False,
                )
                self.assertEqual(rust.returncode, 0, rust.stderr.decode(errors="replace"))
                rust_result = json.loads(rust.stdout)
                self.assertEqual(rust_result["kind"], driver.RESULT_KIND)
                self.assertTrue(rust_result["preview_only"])


if __name__ == "__main__":
    unittest.main()
