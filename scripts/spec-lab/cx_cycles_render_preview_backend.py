#!/usr/bin/env python3
"""Operator-pinned, local-only Cycles backend for render preview protocol v1.

This module is executable only through ``cx_agent_render_preview_driver.py`` and
is intentionally not a production render worker.  It accepts a closed payload,
copies an exact SHA-256-pinned ``.blend`` from an operator-owned scene root into
an unguessable per-process output session, and invokes an operator-pinned
Blender executable with ``--disable-autoexec`` and a fixed Cycles child script.

The cheap proposal and verifier are independent low-sample renders (different
deterministic Cycles seeds).  Pillow computes a bounded, reference-free global
and worst-tile agreement score.  A passing draft is delivered; a failing draft
is repaired by bounded, halo-expanded high-sample borders when the disagreement
is local.  The resulting composite is checked again against the independent
full-frame verifier and escalates to a full high-sample render on any miss.
Callback values are stable relative manifest-path descriptors, so the generic
``SpeculativeEngine`` can perform an exact structural equality check without
comparing volatile timings or bytes.

All artifacts remain synthetic, local, unattested preview evidence.  This
backend does not measure a counterfactual baseline and makes no speedup claim.
No request field can select an executable, Python module, script, command,
device, output path, timeout, seed, or acceptance threshold. The render device
is an operator-owned pin: CPU by default, or Metal when explicitly enabled on
an Apple worker.
"""

from __future__ import annotations

import atexit
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import select
import secrets
import shutil
import signal
import stat
import struct
import subprocess
import time
from typing import Any, NamedTuple

from PIL import Image, ImageChops, ImageStat, UnidentifiedImageError

from cx_speculative_core import DraftProposal, RepairResult, SpecUnit, Verification


PROTOCOL_VERSION = 1
MODALITY = "render"

BLENDER_ENV = "CX_SPEC_RENDER_CYCLES_BLENDER"
BLENDER_SHA_ENV = "CX_SPEC_RENDER_CYCLES_BLENDER_SHA256"
SCENE_ROOT_ENV = "CX_SPEC_RENDER_CYCLES_SCENE_ROOT"
OUTPUT_ROOT_ENV = "CX_SPEC_RENDER_CYCLES_OUTPUT_ROOT"
TIMEOUT_ENV = "CX_SPEC_RENDER_CYCLES_TIMEOUT_SECS"
DEVICE_ENV = "CX_SPEC_RENDER_CYCLES_DEVICE"
LOCAL_PROCESS_GROUP_ENV = "CX_SPEC_RENDER_CYCLES_LOCAL_PROCESS_GROUP"
CANDIDATE_PROFILE_ENV = "CX_SPEC_RENDER_CYCLES_CANDIDATE_PROFILE"
CANDIDATE_PROFILE_SCOPE_ENV = (
    "CX_SPEC_RENDER_CYCLES_CANDIDATE_PROFILE_SCOPE"
)
CANDIDATE_PROFILE_AUTH_ENV = "CX_SPEC_RENDER_CYCLES_CANDIDATE_PROFILE_AUTH"
RESIDENT_POLICY_ENV = "CX_SPEC_RENDER_CYCLES_RESIDENT_POLICY"
CANDIDATE_PROFILE_BENCHMARK_SCOPE = "benchmark_screen_v1"
CANDIDATE_PROFILE_NATIVE_SCOPE = "native_only"
BENCHMARK_PROFILE_META_KEY = "cx_benchmark_profile_auth_v1"
BENCHMARK_PROFILE_UNIT_ID = "local-metal-benchmark"
BACKEND_PIN_ENV = "CX_SPEC_RENDER_PREVIEW_BACKEND_SHA256"
CORE_PIN_ENV = "CX_SPEC_RENDER_PREVIEW_CORE_SHA256"
ADAPTER_PIN_ENV = "CX_SPEC_RENDER_PREVIEW_ADAPTER_SHA256"

DEFAULT_TIMEOUT_SECS = 120
MAX_TIMEOUT_SECS = 600
MIN_DIMENSION = 16
MAX_DIMENSION = 4_096
MAX_PIXELS = 4_194_304
MAX_FRAME = 1_000_000
MAX_LOW_SAMPLES = 64
MAX_HIGH_SAMPLES = 4_096
MAX_SCENE_BYTES = 2 << 30
MAX_SCENE_FILES = 4_096
MAX_ARTIFACT_BYTES = MAX_PIXELS * 8 + (1 << 20)
MAX_SCENE_RELATIVE_BYTES = 1_024
MAX_WORKER_FRAME_BYTES = 16 << 10
MAX_WORKER_COMMANDS = 64
MAX_AGREEMENT_TILES = 8_192
MAX_RECORDED_FAILING_TILES = 128
MAX_SELECTIVE_FAILING_TILES = 64
MAX_SELECTIVE_COMPONENTS = 8
MAX_SELECTIVE_AREA_FRACTION = 0.25
REPAIR_HALO_PIXELS = 4
PIXEL_FILTER_TYPE = "BLACKMAN_HARRIS"
PIXEL_FILTER_WIDTH = 1.5
# Preview PNGs are transient, local transport artifacts. Deflate work is fully
# charged to the product path and buys no image fidelity, so keep the lossless
# pixels while avoiding compression latency. The worker reports this value in
# its handshake and every artifact manifest binds it.
PNG_COMPRESSION = 0
SUPPORTED_DEVICES = frozenset({"CPU", "METAL"})
RESIDENT_POLICY_BROAD = "broad_v1"
RESIDENT_POLICY_SAME_FRAME_MINIMAL = "same_frame_minimal_v1"
RESIDENT_POLICIES = frozenset(
    {RESIDENT_POLICY_BROAD, RESIDENT_POLICY_SAME_FRAME_MINIMAL}
)
CANDIDATE_PROFILES: dict[str, dict[str, int | float | bool | str | None]] = {
    "native": {
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
    "cap16_v1": {
        "name": "cap16_v1",
        "max_bounces": 16,
        "diffuse_bounces": 8,
        "glossy_bounces": 8,
        "transmission_bounces": 16,
        "use_light_tree": None,
        "use_adaptive_sampling": False,
        "adaptive_min_samples": None,
        "adaptive_threshold": None,
    },
    "cap12_v1": {
        "name": "cap12_v1",
        "max_bounces": 12,
        "diffuse_bounces": 6,
        "glossy_bounces": 6,
        "transmission_bounces": 12,
        "use_light_tree": None,
        "use_adaptive_sampling": False,
        "adaptive_min_samples": None,
        "adaptive_threshold": None,
    },
    "cap8_v1": {
        "name": "cap8_v1",
        "max_bounces": 8,
        "diffuse_bounces": 4,
        "glossy_bounces": 4,
        "transmission_bounces": 8,
        "use_light_tree": None,
        "use_adaptive_sampling": False,
        "adaptive_min_samples": None,
        "adaptive_threshold": None,
    },
    "cap8_lighttree_v1": {
        "name": "cap8_lighttree_v1",
        "max_bounces": 8,
        "diffuse_bounces": 4,
        "glossy_bounces": 4,
        "transmission_bounces": 8,
        "use_light_tree": True,
        "use_adaptive_sampling": False,
        "adaptive_min_samples": None,
        "adaptive_threshold": None,
    },
    "cap8_adaptive_v1": {
        "name": "cap8_adaptive_v1",
        "max_bounces": 8,
        "diffuse_bounces": 4,
        "glossy_bounces": 4,
        "transmission_bounces": 8,
        "use_light_tree": None,
        "use_adaptive_sampling": True,
        "adaptive_min_samples": 8,
        "adaptive_threshold": 0.01,
    },
    "cap8_both_v1": {
        "name": "cap8_both_v1",
        "max_bounces": 8,
        "diffuse_bounces": 4,
        "glossy_bounces": 4,
        "transmission_bounces": 8,
        "use_light_tree": True,
        "use_adaptive_sampling": True,
        "adaptive_min_samples": 8,
        "adaptive_threshold": 0.01,
    },
    "cap8_both_relaxed_v1": {
        "name": "cap8_both_relaxed_v1",
        "max_bounces": 8,
        "diffuse_bounces": 4,
        "glossy_bounces": 4,
        "transmission_bounces": 8,
        "use_light_tree": True,
        "use_adaptive_sampling": True,
        "adaptive_min_samples": 8,
        "adaptive_threshold": 0.02,
    },
    "oidn_native_v1": {
        "name": "oidn_native_v1",
        "max_bounces": None,
        "diffuse_bounces": None,
        "glossy_bounces": None,
        "transmission_bounces": None,
        "use_light_tree": None,
        "use_adaptive_sampling": False,
        "adaptive_min_samples": None,
        "adaptive_threshold": None,
    },
}
DENOISING_OFF_POLICY: dict[str, bool | str | None] = {
    "use_denoising": False,
    "view_layer_use_denoising": False,
    "denoiser": None,
    "denoising_input_passes": None,
    "denoising_prefilter": None,
    "denoising_quality": None,
    "denoising_use_gpu": None,
}
OIDN_NATIVE_POLICY: dict[str, bool | str | None] = {
    "use_denoising": True,
    "view_layer_use_denoising": True,
    "denoiser": "OPENIMAGEDENOISE",
    "denoising_input_passes": "RGB_ALBEDO_NORMAL",
    "denoising_prefilter": "ACCURATE",
    "denoising_quality": "HIGH",
    "denoising_use_gpu": False,
}
CANDIDATE_DENOISING_POLICIES = {
    name: dict(
        OIDN_NATIVE_POLICY if name == "oidn_native_v1" else DENOISING_OFF_POLICY
    )
    for name in CANDIDATE_PROFILES
}
# Compare a resolution-normalized grid: at most 16 cells along the long image
# edge, but never make nominal cells smaller than 32 px. A fixed 32 px grid
# made the same scene progressively harder solely because 1080p had 2,040
# chances for one extreme tile while a 512x288 preview had 144.
AGREEMENT_MIN_TILE_EDGE = 32
AGREEMENT_MAX_LONG_EDGE_TILES = 16
MICROTILE_EDGE = 32
# A separate catastrophic-defect sentinel. It is deliberately looser than the
# 0.85 regional preview gate so ordinary Monte Carlo noise does not recreate the
# resolution/multiple-comparisons bug, while a small severe corruption cannot be
# averaged away inside a 120x120 region at 1080p.
MICROTILE_AGREEMENT_MIN = 0.70
GLOBAL_AGREEMENT_MIN = 0.90
# Match the preview tier advertised by cx_agent_render_preview_driver.py.
WORST_TILE_AGREEMENT_MIN = 0.85
ARTIFACT_KIND = "cx_cycles_preview_artifact"
MANIFEST_KIND = "cx_cycles_preview_manifest"
ARTIFACT_SCHEMA_VERSION = 2
VERIFICATION_SCHEMA_VERSION = 2
BENCHMARK_AUDIT_SCHEMA_VERSION = 2
BINDING_POLICY = "render-preview-operator-policy-v2"
WORKER_PROTOCOL = "cx-cycles-preview-worker-v1"

# Pillow must reject unexpectedly large images before allocating their decoded
# pixels.  The backend independently checks the exact requested dimensions.
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class CyclesPreviewError(ValueError):
    """A closed-protocol, content-pin, artifact, or renderer violation."""


# This source is materialized byte-for-byte under the private session root. It
# is not supplied by the request and its digest is checked before every render
# command. Blender receives only two inherited pipe descriptors plus an
# unguessable startup nonce and the private session root. Its ordinary
# stdout/stderr can therefore never corrupt the bounded worker protocol.
_BLENDER_CHILD_SOURCE = r'''import hashlib
import json
import os
from pathlib import Path
import struct
import sys

import bpy

PROTOCOL = "cx-cycles-preview-worker-v1"
MAX_FRAME_BYTES = 16 << 10
MAX_PIXELS = 4_194_304
MAX_COMMANDS = 64
RESIDENT_POLICY_BROAD = "broad_v1"
RESIDENT_POLICY_SAME_FRAME_MINIMAL = "same_frame_minimal_v1"
RESIDENT_POLICIES = {
    RESIDENT_POLICY_BROAD, RESIDENT_POLICY_SAME_FRAME_MINIMAL,
}
CANDIDATE_PROFILES = {
    "native": {
        "name": "native", "max_bounces": None, "diffuse_bounces": None,
        "glossy_bounces": None, "transmission_bounces": None,
        "use_light_tree": None, "use_adaptive_sampling": False,
        "adaptive_min_samples": None, "adaptive_threshold": None,
    },
    "cap16_v1": {
        "name": "cap16_v1", "max_bounces": 16, "diffuse_bounces": 8,
        "glossy_bounces": 8, "transmission_bounces": 16,
        "use_light_tree": None, "use_adaptive_sampling": False,
        "adaptive_min_samples": None, "adaptive_threshold": None,
    },
    "cap12_v1": {
        "name": "cap12_v1", "max_bounces": 12, "diffuse_bounces": 6,
        "glossy_bounces": 6, "transmission_bounces": 12,
        "use_light_tree": None, "use_adaptive_sampling": False,
        "adaptive_min_samples": None, "adaptive_threshold": None,
    },
    "cap8_v1": {
        "name": "cap8_v1", "max_bounces": 8, "diffuse_bounces": 4,
        "glossy_bounces": 4, "transmission_bounces": 8,
        "use_light_tree": None, "use_adaptive_sampling": False,
        "adaptive_min_samples": None, "adaptive_threshold": None,
    },
    "cap8_lighttree_v1": {
        "name": "cap8_lighttree_v1", "max_bounces": 8, "diffuse_bounces": 4,
        "glossy_bounces": 4, "transmission_bounces": 8,
        "use_light_tree": True, "use_adaptive_sampling": False,
        "adaptive_min_samples": None, "adaptive_threshold": None,
    },
    "cap8_adaptive_v1": {
        "name": "cap8_adaptive_v1", "max_bounces": 8, "diffuse_bounces": 4,
        "glossy_bounces": 4, "transmission_bounces": 8,
        "use_light_tree": None, "use_adaptive_sampling": True,
        "adaptive_min_samples": 8, "adaptive_threshold": 0.01,
    },
    "cap8_both_v1": {
        "name": "cap8_both_v1", "max_bounces": 8, "diffuse_bounces": 4,
        "glossy_bounces": 4, "transmission_bounces": 8,
        "use_light_tree": True, "use_adaptive_sampling": True,
        "adaptive_min_samples": 8, "adaptive_threshold": 0.01,
    },
    "cap8_both_relaxed_v1": {
        "name": "cap8_both_relaxed_v1", "max_bounces": 8, "diffuse_bounces": 4,
        "glossy_bounces": 4, "transmission_bounces": 8,
        "use_light_tree": True, "use_adaptive_sampling": True,
        "adaptive_min_samples": 8, "adaptive_threshold": 0.02,
    },
    "oidn_native_v1": {
        "name": "oidn_native_v1", "max_bounces": None, "diffuse_bounces": None,
        "glossy_bounces": None, "transmission_bounces": None,
        "use_light_tree": None, "use_adaptive_sampling": False,
        "adaptive_min_samples": None, "adaptive_threshold": None,
    },
}
DENOISING_OFF_POLICY = {
    "use_denoising": False,
    "view_layer_use_denoising": False,
    "denoiser": None,
    "denoising_input_passes": None,
    "denoising_prefilter": None,
    "denoising_quality": None,
    "denoising_use_gpu": None,
}
OIDN_NATIVE_POLICY = {
    "use_denoising": True,
    "view_layer_use_denoising": True,
    "denoiser": "OPENIMAGEDENOISE",
    "denoising_input_passes": "RGB_ALBEDO_NORMAL",
    "denoising_prefilter": "ACCURATE",
    "denoising_quality": "HIGH",
    "denoising_use_gpu": False,
}
CANDIDATE_DENOISING_POLICIES = {
    name: dict(
        OIDN_NATIVE_POLICY if name == "oidn_native_v1" else DENOISING_OFF_POLICY
    )
    for name in CANDIDATE_PROFILES
}
candidate_profile_name = os.environ.get(
    "CX_CYCLES_CANDIDATE_PROFILE", "native"
)
if candidate_profile_name not in CANDIDATE_PROFILES:
    raise RuntimeError("unsupported operator-pinned candidate profile")
SELECTED_CANDIDATE_PROFILE = CANDIDATE_PROFILES[candidate_profile_name]
SELECTED_DENOISING_POLICY = CANDIDATE_DENOISING_POLICIES[
    candidate_profile_name
]
SELECTED_RESIDENT_POLICY = os.environ.get(
    "CX_CYCLES_RESIDENT_POLICY", RESIDENT_POLICY_BROAD
)
if SELECTED_RESIDENT_POLICY not in RESIDENT_POLICIES:
    raise RuntimeError("unsupported operator-pinned resident policy")

def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")

def emit(writer, value):
    encoded = canonical(value) + b"\n"
    if len(encoded) > MAX_FRAME_BYTES:
        raise RuntimeError("Cycles worker response exceeded its fixed bound")
    writer.write(encoded)
    writer.flush()

def bounded_int(value, name, minimum, maximum):
    if not isinstance(value, int) or isinstance(value, bool):
        raise RuntimeError(name + " must be an integer")
    if not minimum <= value <= maximum:
        raise RuntimeError(name + " is outside its fixed range")
    return value

def parse_command(raw, expected_id, private_root):
    if not raw.endswith(b"\n"):
        raise RuntimeError("Cycles worker command was not newline terminated")
    try:
        frame = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Cycles worker command was not strict JSON") from exc
    if not isinstance(frame, dict) or set(frame) != {
        "command", "command_sha256", "kind", "schema_version"
    }:
        raise RuntimeError("Cycles worker frame shape mismatch")
    if frame["schema_version"] != 1 or frame["kind"] != "render":
        raise RuntimeError("Cycles worker frame version/kind mismatch")
    command = frame["command"]
    if not isinstance(command, dict) or set(command) != {
        "border", "candidate_denoising_policy", "candidate_profile", "command_id",
        "frame", "height", "output", "phase", "resident_policy", "sample_offset",
        "samples", "seed", "width"
    }:
        raise RuntimeError("Cycles worker command shape mismatch")
    command_id = bounded_int(command["command_id"], "command_id", 1, MAX_COMMANDS)
    if command_id != expected_id:
        raise RuntimeError("Cycles worker command id mismatch")
    digest = frame["command_sha256"]
    if not isinstance(digest, str) or len(digest) != 64:
        raise RuntimeError("Cycles worker command digest shape mismatch")
    actual = hashlib.sha256(canonical(command)).hexdigest()
    if digest != actual:
        raise RuntimeError("Cycles worker command digest mismatch")
    if command["phase"] not in {"draft", "verify", "repair", "baseline"}:
        raise RuntimeError("Cycles worker phase mismatch")
    width = bounded_int(command["width"], "width", 16, 4096)
    height = bounded_int(command["height"], "height", 16, 4096)
    if width * height > MAX_PIXELS:
        raise RuntimeError("Cycles worker pixel bound exceeded")
    bounded_int(command["frame"], "frame", 0, 1_000_000)
    bounded_int(command["samples"], "samples", 1, 4096)
    bounded_int(command["sample_offset"], "sample_offset", 0, 4096)
    bounded_int(command["seed"], "seed", 0, 0x7fffffff)
    if command["candidate_profile"] != SELECTED_CANDIDATE_PROFILE:
        raise RuntimeError("Cycles worker candidate profile mismatch")
    if command["candidate_denoising_policy"] != SELECTED_DENOISING_POLICY:
        raise RuntimeError("Cycles worker candidate denoising policy mismatch")
    if command["resident_policy"] != SELECTED_RESIDENT_POLICY:
        raise RuntimeError("Cycles worker resident policy mismatch")
    border = command["border"]
    if border is not None:
        if not isinstance(border, list) or len(border) != 4:
            raise RuntimeError("Cycles worker border shape mismatch")
        if any(not isinstance(value, int) or isinstance(value, bool) for value in border):
            raise RuntimeError("Cycles worker border values must be integers")
        left, top, right, bottom = border
        if not (0 <= left < right <= width and 0 <= top < bottom <= height):
            raise RuntimeError("Cycles worker border is outside the render")
    output_raw = command["output"]
    if not isinstance(output_raw, str) or not output_raw:
        raise RuntimeError("Cycles worker output path mismatch")
    output = Path(output_raw)
    if not output.is_absolute() or output.exists() or output.is_symlink():
        raise RuntimeError("Cycles worker output path is unsafe")
    try:
        output.parent.resolve(strict=True).relative_to(private_root)
    except (OSError, ValueError) as exc:
        raise RuntimeError("Cycles worker output escaped its private root") from exc
    if output.parent.is_symlink():
        raise RuntimeError("Cycles worker output parent is a symlink")
    return command, digest

if "--" not in sys.argv:
    raise RuntimeError("missing fixed Cycles worker descriptors")
args = sys.argv[sys.argv.index("--") + 1:]
if len(args) != 4:
    raise RuntimeError("expected fixed Cycles worker descriptor tuple")
command_fd = int(args[0], 10)
response_fd = int(args[1], 10)
nonce = args[2]
private_root = Path(args[3]).resolve(strict=True)
if len(nonce) != 64 or any(ch not in "0123456789abcdef" for ch in nonce):
    raise RuntimeError("Cycles worker startup nonce mismatch")

with (
    os.fdopen(command_fd, "rb", buffering=0) as reader,
    os.fdopen(response_fd, "wb", buffering=0) as writer,
):
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    view_layers = list(scene.view_layers)
    if not view_layers:
        raise RuntimeError("Cycles scene has no render view layer")
    allowed_scene_root = (private_root / "scenes").resolve(strict=True)
    native_integrator = {
        "max_bounces": int(scene.cycles.max_bounces),
        "diffuse_bounces": int(scene.cycles.diffuse_bounces),
        "glossy_bounces": int(scene.cycles.glossy_bounces),
        "transmission_bounces": int(scene.cycles.transmission_bounces),
    }

    def assign_property(owner, name, value, *, only_if_different):
        if only_if_different:
            actual = getattr(owner, name)
            expected = value
            if isinstance(actual, float) and isinstance(value, float):
                expected = struct.unpack("f", struct.pack("f", value))[0]
            if actual == expected:
                return
        setattr(owner, name, value)

    def apply_integrator(values, *, only_if_different=False):
        for key, value in values.items():
            assign_property(
                scene.cycles, key, int(value),
                only_if_different=only_if_different,
            )

    def require_property(owner, name, value):
        if not hasattr(owner, name):
            raise RuntimeError("required Cycles denoising property is unavailable: " + name)
        if getattr(owner, name) == value:
            return
        try:
            setattr(owner, name, value)
        except BaseException as exc:
            raise RuntimeError(
                "could not apply required Cycles denoising property " + name
            ) from exc
        actual = getattr(owner, name)
        if actual != value:
            raise RuntimeError(
                "Cycles did not retain required denoising property " + name
            )

    def actual_denoising_policy():
        use_denoising = bool(scene.cycles.use_denoising)
        layer_values = []
        for layer in view_layers:
            if not hasattr(layer.cycles, "use_denoising"):
                if use_denoising:
                    raise RuntimeError(
                        "required Cycles view-layer denoising property is unavailable"
                    )
                layer_values.append(False)
            else:
                layer_values.append(bool(layer.cycles.use_denoising))
        view_layer_use_denoising = all(layer_values)
        if not use_denoising and not any(layer_values):
            return dict(DENOISING_OFF_POLICY)
        required = (
            "denoiser",
            "denoising_input_passes",
            "denoising_prefilter",
            "denoising_quality",
            "denoising_use_gpu",
        )
        missing = [name for name in required if not hasattr(scene.cycles, name)]
        if missing:
            raise RuntimeError(
                "required Cycles denoising properties are unavailable: "
                + ",".join(missing)
            )
        return {
            "use_denoising": use_denoising,
            "view_layer_use_denoising": view_layer_use_denoising,
            "denoiser": str(scene.cycles.denoiser),
            "denoising_input_passes": str(scene.cycles.denoising_input_passes),
            "denoising_prefilter": str(scene.cycles.denoising_prefilter),
            "denoising_quality": str(scene.cycles.denoising_quality),
            "denoising_use_gpu": bool(scene.cycles.denoising_use_gpu),
        }

    def apply_denoising(policy):
        if policy["use_denoising"]:
            for name in (
                "denoiser",
                "denoising_input_passes",
                "denoising_prefilter",
                "denoising_quality",
                "denoising_use_gpu",
            ):
                require_property(scene.cycles, name, policy[name])
            for layer in view_layers:
                require_property(
                    layer.cycles,
                    "use_denoising",
                    policy["view_layer_use_denoising"],
                )
            require_property(scene.cycles, "use_denoising", True)
        else:
            require_property(scene.cycles, "use_denoising", False)
            for layer in view_layers:
                if hasattr(layer.cycles, "use_denoising"):
                    require_property(layer.cycles, "use_denoising", False)
        actual = actual_denoising_policy()
        if actual != policy:
            raise RuntimeError("Cycles did not apply the pinned denoising policy")
        return actual

    bundled_dependencies = []

    def require_bundled_dependency(label, raw_path, *, library=None, packed=False):
        if packed or not raw_path or raw_path == "<builtin>":
            return
        try:
            resolved = Path(
                bpy.path.abspath(raw_path, library=library)
            ).resolve(strict=True)
            relative = resolved.relative_to(allowed_scene_root)
        except (OSError, ValueError) as exc:
            raise RuntimeError(label + " dependency is missing or outside pinned bundle") from exc
        bundled_dependencies.append(relative.as_posix())

    for library in bpy.data.libraries:
        require_bundled_dependency("library", library.filepath)
    for image in bpy.data.images:
        if image.source == "FILE":
            require_bundled_dependency(
                "image", image.filepath, library=image.library,
                packed=bool(image.packed_file),
            )
    for collection_name in ("movieclips", "sounds", "fonts", "cache_files", "volumes"):
        for block in getattr(bpy.data, collection_name, ()):
            require_bundled_dependency(
                collection_name,
                getattr(block, "filepath", ""),
                library=getattr(block, "library", None),
                packed=bool(getattr(block, "packed_file", None)),
            )
    requested_device = os.environ.get("CX_CYCLES_DEVICE", "CPU")
    if requested_device not in {"CPU", "METAL"}:
        raise RuntimeError("unsupported operator-pinned Cycles device")
    if requested_device == "METAL":
        prefs = bpy.context.preferences.addons["cycles"].preferences
        prefs.compute_device_type = "METAL"
        prefs.get_devices()
        if hasattr(prefs, "get_devices_for_type"):
            prefs.get_devices_for_type("METAL")
        metal_devices = [
            device for device in prefs.devices
            if getattr(device, "type", "") == "METAL"
        ]
        if not metal_devices:
            raise RuntimeError("operator pinned METAL but Cycles enumerated no Metal GPU")
        for device in prefs.devices:
            device.use = device in metal_devices
        scene.cycles.device = "GPU"
        actual_device = "GPU/METAL"
        enabled_device_names = sorted(
            str(getattr(device, "name", "")) for device in metal_devices
        )
    else:
        scene.cycles.device = "CPU"
        actual_device = "CPU"
        enabled_device_names = ["CPU"]
    scene.cycles.use_animated_seed = False
    scene.cycles.use_adaptive_sampling = False
    reference_denoising_policy = apply_denoising(DENOISING_OFF_POLICY)
    # A selected OIDN profile must prove every required RNA property at worker
    # startup. Restore the reference policy before accepting any commands.
    if SELECTED_DENOISING_POLICY["use_denoising"]:
        apply_denoising(SELECTED_DENOISING_POLICY)
        apply_denoising(reference_denoising_policy)
    scene.cycles.pixel_filter_type = "BLACKMAN_HARRIS"
    scene.cycles.filter_width = 1.5
    scene.render.use_persistent_data = True
    scene.render.filter_size = 1.5
    scene.render.resolution_percentage = 100
    scene.render.use_border = False
    scene.render.use_crop_to_border = False
    scene.render.use_file_extension = True
    scene.render.use_overwrite = True
    scene.render.use_multiview = False
    scene.render.use_compositing = False
    scene.render.use_sequencer = False
    scene.render.use_freestyle = False
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.image_settings.color_depth = "8"
    scene.render.image_settings.compression = 0
    reference_sampling = {
        "use_light_tree": bool(scene.cycles.use_light_tree),
        "use_adaptive_sampling": False,
        "adaptive_min_samples": int(scene.cycles.adaptive_min_samples),
        "adaptive_threshold": float(scene.cycles.adaptive_threshold),
    }

    def apply_sampling(values, *, only_if_different=False):
        for name, value in (
            ("use_light_tree", bool(values["use_light_tree"])),
            ("use_adaptive_sampling", bool(values["use_adaptive_sampling"])),
            ("adaptive_min_samples", int(values["adaptive_min_samples"])),
            ("adaptive_threshold", float(values["adaptive_threshold"])),
        ):
            assign_property(
                scene.cycles, name, value,
                only_if_different=only_if_different,
            )
    build_hash = bpy.app.build_hash
    if isinstance(build_hash, bytes):
        build_hash = build_hash.decode("ascii", errors="replace")
    dependency_paths = sorted(set(bundled_dependencies))
    emit(writer, {
        "blender_build_hash": str(build_hash),
        "blender_version": str(bpy.app.version_string),
        "dependency_count": len(dependency_paths),
        "dependency_paths_sha256": hashlib.sha256(
            canonical(dependency_paths)
        ).hexdigest(),
        "candidate_profile": SELECTED_CANDIDATE_PROFILE,
        "candidate_denoising_policy": SELECTED_DENOISING_POLICY,
        "resident_policy": SELECTED_RESIDENT_POLICY,
        "png_compression": int(scene.render.image_settings.compression),
        "native_integrator": native_integrator,
        "reference_sampling": reference_sampling,
        "reference_denoising_policy": reference_denoising_policy,
        "device": actual_device,
        "enabled_device_names": enabled_device_names,
        "kind": "ready",
        "nonce": nonce,
        "protocol": PROTOCOL,
        "scene_path": str(Path(bpy.data.filepath).resolve(strict=True)),
        "schema_version": 1,
    })

    expected_id = 1
    last_successful_frame = None
    while expected_id <= MAX_COMMANDS:
        raw = reader.readline(MAX_FRAME_BYTES + 1)
        if not raw:
            break
        digest = None
        command_id = expected_id
        resident_mutation = None
        try:
            if len(raw) > MAX_FRAME_BYTES:
                raise RuntimeError("Cycles worker command exceeded its fixed bound")
            cfg, digest = parse_command(raw, expected_id, private_root)
            command_id = cfg["command_id"]
            same_frame_minimal = (
                SELECTED_RESIDENT_POLICY == RESIDENT_POLICY_SAME_FRAME_MINIMAL
                and last_successful_frame == cfg["frame"]
            )
            resident_mutation = (
                RESIDENT_POLICY_SAME_FRAME_MINIMAL
                if same_frame_minimal else RESIDENT_POLICY_BROAD
            )
            assign_property(
                scene.cycles, "samples", cfg["samples"],
                only_if_different=same_frame_minimal,
            )
            assign_property(
                scene.render, "resolution_x", cfg["width"],
                only_if_different=same_frame_minimal,
            )
            assign_property(
                scene.render, "resolution_y", cfg["height"],
                only_if_different=same_frame_minimal,
            )
            border = cfg["border"]
            if border is None:
                border_values = {
                    "use_border": False,
                    "border_min_x": 0.0,
                    "border_max_x": 1.0,
                    "border_min_y": 0.0,
                    "border_max_y": 1.0,
                }
            else:
                left, top, right, bottom = border
                border_values = {
                    "use_border": True,
                    "use_crop_to_border": False,
                    "border_min_x": left / cfg["width"],
                    "border_max_x": right / cfg["width"],
                    "border_min_y": (cfg["height"] - bottom) / cfg["height"],
                    "border_max_y": (cfg["height"] - top) / cfg["height"],
                }
            for name, value in border_values.items():
                assign_property(
                    scene.render, name, value,
                    only_if_different=same_frame_minimal,
                )
            assign_property(
                scene.render, "filepath", cfg["output"],
                only_if_different=same_frame_minimal,
            )
            if not same_frame_minimal:
                scene.frame_set(cfg["frame"])
            # A prior candidate command may have lowered integrator limits in
            # this resident scene. Restore the exact scene-open integrator and
            # fixed non-adaptive sampling policy after frame evaluation, then
            # apply experimental caps only to draft/verify. Repair and the
            # measurement baseline always use this explicit reference policy.
            caps = cfg["candidate_profile"]
            desired_integrator = dict(native_integrator)
            desired_sampling = dict(reference_sampling)
            if cfg["phase"] in {"draft", "verify"}:
                desired_integrator = {
                    key: min(native_integrator[key], caps[key])
                    if caps[key] is not None else native_integrator[key]
                    for key in native_integrator
                }
                if caps["use_light_tree"] is not None:
                    desired_sampling["use_light_tree"] = bool(
                        caps["use_light_tree"]
                    )
                if caps["use_adaptive_sampling"]:
                    desired_sampling["use_adaptive_sampling"] = True
                    desired_sampling["adaptive_min_samples"] = int(
                        caps["adaptive_min_samples"]
                    )
                    desired_sampling["adaptive_threshold"] = float(
                        caps["adaptive_threshold"]
                    )
            if same_frame_minimal:
                apply_integrator(desired_integrator, only_if_different=True)
                apply_sampling(desired_sampling, only_if_different=True)
            else:
                apply_integrator(native_integrator)
                apply_sampling(reference_sampling)
                if cfg["phase"] in {"draft", "verify"}:
                    apply_integrator(desired_integrator)
                    if caps["use_light_tree"] is not None:
                        scene.cycles.use_light_tree = bool(caps["use_light_tree"])
                    if caps["use_adaptive_sampling"]:
                        scene.cycles.use_adaptive_sampling = True
                        scene.cycles.adaptive_min_samples = int(
                            caps["adaptive_min_samples"]
                        )
                        scene.cycles.adaptive_threshold = float(
                            caps["adaptive_threshold"]
                        )
            apply_denoising(
                cfg["candidate_denoising_policy"]
                if cfg["phase"] in {"draft", "verify"}
                else reference_denoising_policy
            )
            actual_integrator = {
                key: int(getattr(scene.cycles, key)) for key in native_integrator
            }
            actual_sampling = {
                "use_light_tree": bool(scene.cycles.use_light_tree),
                "use_adaptive_sampling": bool(scene.cycles.use_adaptive_sampling),
                "adaptive_min_samples": int(scene.cycles.adaptive_min_samples),
                "adaptive_threshold": float(scene.cycles.adaptive_threshold),
            }
            actual_denoising = actual_denoising_policy()
            # frame_set() evaluates scene animation and may restore render
            # properties from the evaluated frame. Apply the independent
            # integrator seed afterwards so the verifier's seed survives.
            assign_property(
                scene.cycles, "seed", cfg["seed"],
                only_if_different=same_frame_minimal,
            )
            # A disjoint sample range is a stronger independence boundary than
            # seed metadata alone. Cycles' Sample Offset is specifically meant
            # for splitting sample ranges across renders.
            assign_property(
                scene.cycles, "sample_offset", cfg["sample_offset"],
                only_if_different=same_frame_minimal,
            )
            # Persistent Cycles data keeps the expensive scene/BVH resident, but
            # integrator-only changes still need an explicit depsgraph tag in a
            # long-lived Python worker. Without it, consecutive renders at the
            # same frame can reuse the previous seed and make an "independent"
            # low-SPP verifier pixel-identical to the draft.
            if not same_frame_minimal:
                scene.update_tag()
            bpy.context.view_layer.update()
            bpy.ops.render.render(write_still=True)
            last_successful_frame = cfg["frame"]
            emit(writer, {
                "command_id": command_id,
                "command_sha256": digest,
                "integrator": actual_integrator,
                "denoising": actual_denoising,
                "kind": "render_result",
                "ok": True,
                "resident_mutation": resident_mutation,
                "resident_policy": SELECTED_RESIDENT_POLICY,
                "sampling": actual_sampling,
                "schema_version": 1,
            })
        except BaseException as exc:
            emit(writer, {
                "command_id": command_id,
                "command_sha256": digest,
                "error": (type(exc).__name__ + ": " + str(exc))[:500],
                "kind": "render_result",
                "ok": False,
                "resident_mutation": resident_mutation,
                "resident_policy": SELECTED_RESIDENT_POLICY,
                "schema_version": 1,
            })
            raise
        expected_id += 1
'''
_BLENDER_CHILD_BYTES = _BLENDER_CHILD_SOURCE.encode("utf-8")
_BLENDER_CHILD_SHA256 = hashlib.sha256(_BLENDER_CHILD_BYTES).hexdigest()

_SESSION: dict[str, Any] | None = None
_CONTEXTS: dict[str, dict[str, Any]] = {}
_BINDINGS: dict[str, str] = {}
_VERIFICATIONS: dict[str, tuple[dict[str, Any], Verification]] = {}
_SCENE_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
_WORKER: dict[str, Any] | None = None

_RETAINED_VALIDATED_PNGS_KEY = "retained_validated_pngs"
_RETAINABLE_PNG_PHASES = frozenset({"draft", "verify"})
_MAX_RETAINED_VALIDATED_PNGS = len(_RETAINABLE_PNG_PHASES)


class _ValidatedPngSnapshot(NamedTuple):
    """One immutable file snapshot and the decode made from those bytes."""

    path: Path
    width: int
    height: int
    mode: str
    source_bytes: bytes
    pixel_bytes: bytes | None
    sha256: str
    file_identity: tuple[int, ...]


class RetainedValidatedPng(NamedTuple):
    """A request-bound, one-shot handoff of already validated PNG pixels."""

    phase: str
    path: Path
    width: int
    height: int
    mode: str
    source_bytes: bytes
    pixel_bytes: bytes
    sha256: str
    pixel_sha256: str
    file_identity: tuple[int, ...]
    context_binding_sha256: str
    binding_sha256: str


def _sha256_file(path: Path, *, max_bytes: int | None = None) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise CyclesPreviewError(
                    f"file {path} exceeds the {max_bytes}-byte preview limit"
                )
            digest.update(chunk)
    return digest.hexdigest(), total


def _identity_from_stat(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
        info.st_gid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _regular_file_identity(path: Path) -> tuple[int, ...]:
    """Cheap tamper sentinel captured only after a full content hash succeeds.

    Device/inode plus immutable-to-unprivileged-callers size, mode, ownership,
    link count, mtime and ctime let each render perform pre/post checks without
    rereading hundreds of megabytes. The initial SHA-256 remains authoritative.
    """
    try:
        info = path.lstat()
    except OSError as exc:
        raise CyclesPreviewError(f"cannot stat pinned regular file {path}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise CyclesPreviewError("pinned file changed type or became a symlink")
    return _identity_from_stat(info)


def _sha256_regular_file_with_identity(
    path: Path, *, max_bytes: int | None = None
) -> tuple[str, int, tuple[int, ...]]:
    """Hash one no-follow descriptor and atomically bind its stat sentinel."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CyclesPreviewError(f"cannot open pinned regular file {path}: {exc}") from exc
    digest = hashlib.sha256()
    total = 0
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise CyclesPreviewError("pinned hash input is not a regular file")
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise CyclesPreviewError(
                    f"file {path} exceeds the {max_bytes}-byte preview limit"
                )
            digest.update(chunk)
        after = os.fstat(fd)
        identity = _identity_from_stat(after)
        if _identity_from_stat(before) != identity or total != after.st_size:
            raise CyclesPreviewError("pinned file changed while it was being hashed")
    finally:
        os.close(fd)
    if _regular_file_identity(path) != identity:
        raise CyclesPreviewError("pinned file path changed after it was hashed")
    return digest.hexdigest(), total, identity


def _directory_identity(path: Path) -> tuple[int, ...]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise CyclesPreviewError(f"cannot stat pinned directory {path}: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise CyclesPreviewError("pinned directory changed type or became a symlink")
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
        info.st_gid,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _required_sha256(name: str) -> str:
    value = os.environ.get(name, "")
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise CyclesPreviewError(
            f"{name} must be exactly 64 lowercase hexadecimal characters"
        )
    return value


def _required_absolute(name: str, *, directory: bool) -> Path:
    raw = os.environ.get(name, "")
    if not raw:
        raise CyclesPreviewError(f"{name} is required")
    path = Path(raw)
    if not path.is_absolute():
        raise CyclesPreviewError(f"{name} must be an absolute path")
    try:
        canonical = path.resolve(strict=True)
    except OSError as exc:
        raise CyclesPreviewError(f"cannot resolve {name}={path}: {exc}") from exc
    if directory and not canonical.is_dir():
        raise CyclesPreviewError(f"{name} must name a directory")
    if not directory and not canonical.is_file():
        raise CyclesPreviewError(f"{name} must name a regular file")
    return canonical


def _operator_snapshot() -> tuple[str, ...]:
    return (
        os.environ.get(BLENDER_ENV, ""),
        os.environ.get(BLENDER_SHA_ENV, ""),
        os.environ.get(SCENE_ROOT_ENV, ""),
        os.environ.get(OUTPUT_ROOT_ENV, ""),
        os.environ.get(TIMEOUT_ENV, ""),
        os.environ.get(DEVICE_ENV, ""),
        os.environ.get(LOCAL_PROCESS_GROUP_ENV, ""),
        os.environ.get(CANDIDATE_PROFILE_ENV, ""),
        os.environ.get(CANDIDATE_PROFILE_SCOPE_ENV, ""),
        os.environ.get(CANDIDATE_PROFILE_AUTH_ENV, ""),
        os.environ.get(RESIDENT_POLICY_ENV, ""),
        os.environ.get(BACKEND_PIN_ENV, ""),
        os.environ.get(CORE_PIN_ENV, ""),
        os.environ.get(ADAPTER_PIN_ENV, ""),
    )


def _load_operator_config() -> dict[str, Any]:
    blender = _required_absolute(BLENDER_ENV, directory=False)
    expected_blender_sha = _required_sha256(BLENDER_SHA_ENV)
    try:
        mode = blender.stat().st_mode
    except OSError as exc:
        raise CyclesPreviewError(f"cannot stat Blender executable {blender}: {exc}") from exc
    if mode & 0o111 == 0:
        raise CyclesPreviewError(f"{BLENDER_ENV} is not executable")
    actual_blender_sha, _, blender_file_identity = (
        _sha256_regular_file_with_identity(blender)
    )
    if actual_blender_sha != expected_blender_sha:
        raise CyclesPreviewError(
            f"{BLENDER_SHA_ENV} mismatch: expected {expected_blender_sha}, "
            f"got {actual_blender_sha}"
        )

    scene_root = _required_absolute(SCENE_ROOT_ENV, directory=True)
    output_root = _required_absolute(OUTPUT_ROOT_ENV, directory=True)
    try:
        output_root.relative_to(scene_root)
    except ValueError:
        pass
    else:
        raise CyclesPreviewError("scene and output roots must not overlap")
    try:
        scene_root.relative_to(output_root)
    except ValueError:
        pass
    else:
        raise CyclesPreviewError("scene and output roots must not overlap")
    raw_timeout = os.environ.get(TIMEOUT_ENV, str(DEFAULT_TIMEOUT_SECS))
    try:
        timeout = int(raw_timeout, 10)
    except ValueError as exc:
        raise CyclesPreviewError(
            f"{TIMEOUT_ENV} must be an integer in [1,{MAX_TIMEOUT_SECS}]"
        ) from exc
    if not 1 <= timeout <= MAX_TIMEOUT_SECS:
        raise CyclesPreviewError(
            f"{TIMEOUT_ENV} must be in [1,{MAX_TIMEOUT_SECS}], got {timeout}"
        )
    device = os.environ.get(DEVICE_ENV, "CPU").upper()
    if device not in SUPPORTED_DEVICES:
        raise CyclesPreviewError(
            f"{DEVICE_ENV} must be one of {sorted(SUPPORTED_DEVICES)}, got {device!r}"
        )
    local_group_raw = os.environ.get(LOCAL_PROCESS_GROUP_ENV, "0")
    if local_group_raw not in {"0", "1"}:
        raise CyclesPreviewError(f"{LOCAL_PROCESS_GROUP_ENV} must be 0 or 1")
    candidate_profile_name = os.environ.get(CANDIDATE_PROFILE_ENV, "native")
    if candidate_profile_name not in CANDIDATE_PROFILES:
        raise CyclesPreviewError(
            f"{CANDIDATE_PROFILE_ENV} must be one of "
            f"{sorted(CANDIDATE_PROFILES)}, got {candidate_profile_name!r}"
        )
    candidate_profile_scope = os.environ.get(
        CANDIDATE_PROFILE_SCOPE_ENV, CANDIDATE_PROFILE_NATIVE_SCOPE
    )
    if candidate_profile_scope not in {
        CANDIDATE_PROFILE_NATIVE_SCOPE,
        CANDIDATE_PROFILE_BENCHMARK_SCOPE,
    }:
        raise CyclesPreviewError(
            f"{CANDIDATE_PROFILE_SCOPE_ENV} must be one of "
            f"{[CANDIDATE_PROFILE_BENCHMARK_SCOPE, CANDIDATE_PROFILE_NATIVE_SCOPE]}, "
            f"got {candidate_profile_scope!r}"
        )
    if (
        candidate_profile_name != "native"
        and candidate_profile_scope != CANDIDATE_PROFILE_BENCHMARK_SCOPE
    ):
        raise CyclesPreviewError(
            "non-native candidate profiles are restricted to the explicit "
            "benchmark-screen scope"
        )
    candidate_profile_auth = os.environ.get(CANDIDATE_PROFILE_AUTH_ENV, "")
    resident_policy = os.environ.get(RESIDENT_POLICY_ENV, RESIDENT_POLICY_BROAD)
    if resident_policy not in RESIDENT_POLICIES:
        raise CyclesPreviewError(
            f"{RESIDENT_POLICY_ENV} must be one of "
            f"{sorted(RESIDENT_POLICIES)}, got {resident_policy!r}"
        )
    private_benchmark_policy = (
        candidate_profile_name != "native"
        or resident_policy != RESIDENT_POLICY_BROAD
    )
    if (
        resident_policy != RESIDENT_POLICY_BROAD
        and candidate_profile_scope != CANDIDATE_PROFILE_BENCHMARK_SCOPE
    ):
        raise CyclesPreviewError(
            "non-broad resident policies are restricted to the explicit "
            "benchmark-screen scope"
        )
    if private_benchmark_policy and (
        len(candidate_profile_auth) != 64
        or any(
            character not in "0123456789abcdef"
            for character in candidate_profile_auth
        )
    ):
        raise CyclesPreviewError(
            f"{CANDIDATE_PROFILE_AUTH_ENV} must be a 64-character "
            "benchmark capability for private benchmark policies"
        )
    return {
        "blender": blender,
        "blender_sha256": expected_blender_sha,
        "blender_file_identity": blender_file_identity,
        "scene_root": scene_root,
        "output_root": output_root,
        "timeout": timeout,
        "device": device,
        "local_process_group": local_group_raw == "1",
        "candidate_profile": dict(CANDIDATE_PROFILES[candidate_profile_name]),
        "candidate_denoising_policy": dict(
            CANDIDATE_DENOISING_POLICIES[candidate_profile_name]
        ),
        "candidate_profile_scope": candidate_profile_scope,
        "candidate_profile_auth": candidate_profile_auth,
        "resident_policy": resident_policy,
        "backend_sha256": _required_sha256(BACKEND_PIN_ENV),
        "controller_core_sha256": _required_sha256(CORE_PIN_ENV),
        "controller_adapter_sha256": _required_sha256(ADAPTER_PIN_ENV),
    }


def _write_new_file(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, mode)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise CyclesPreviewError("value is not finite canonical JSON") from exc


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _reject_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _write_manifest(path: Path, value: dict[str, Any]) -> None:
    encoded = _canonical_json(value) + b"\n"
    if len(encoded) > 256 << 10:
        raise CyclesPreviewError("Cycles preview manifest exceeds 256 KiB")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(16)}.tmp")
    _write_new_file(temporary, encoded)
    try:
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _create_private_directory(parent: Path, prefix: str) -> Path:
    for _ in range(32):
        candidate = parent / f"{prefix}{secrets.token_hex(16)}"
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return candidate.resolve(strict=True)
    raise CyclesPreviewError("could not allocate a private Cycles preview session")


def _get_session() -> dict[str, Any]:
    global _SESSION
    snapshot = _operator_snapshot()
    if _SESSION is not None:
        if snapshot != _SESSION["operator_snapshot"]:
            raise CyclesPreviewError("operator Cycles configuration changed during a request")
        return _SESSION

    config = _load_operator_config()
    root = _create_private_directory(config["output_root"], "cycles-preview-")
    scenes = root / "scenes"
    units = root / "units"
    temporary = root / "tmp"
    scenes.mkdir(mode=0o700)
    units.mkdir(mode=0o700)
    temporary.mkdir(mode=0o700)
    child_script = root / "fixed_cycles_child.py"
    _write_new_file(child_script, _BLENDER_CHILD_BYTES)
    actual_script_sha, _, child_script_file_identity = (
        _sha256_regular_file_with_identity(child_script)
    )
    if actual_script_sha != _BLENDER_CHILD_SHA256:
        raise CyclesPreviewError("materialized fixed Cycles child script hash mismatch")
    _SESSION = {
        **config,
        "operator_snapshot": snapshot,
        "root": root,
        "scenes": scenes,
        "units": units,
        "tmp": temporary,
        "child_script": child_script,
        "child_script_file_identity": child_script_file_identity,
        "relative_root": root.relative_to(config["output_root"]),
    }
    return _SESSION


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise CyclesPreviewError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise CyclesPreviewError(f"{name} must be in [{minimum},{maximum}]")
    return value


def _strict_relative_blend(value: Any) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise CyclesPreviewError("scene_path must be a nonempty relative string")
    if len(value.encode("utf-8")) > MAX_SCENE_RELATIVE_BYTES:
        raise CyclesPreviewError(
            f"scene_path exceeds {MAX_SCENE_RELATIVE_BYTES} UTF-8 bytes"
        )
    if "\\" in value or "\x00" in value:
        raise CyclesPreviewError("scene_path must use strict POSIX relative components")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise CyclesPreviewError("scene_path must be relative")
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise CyclesPreviewError("scene_path cannot contain empty, dot, or parent components")
    if not path.parts:
        raise CyclesPreviewError("scene_path must be relative")
    if path.suffix != ".blend":
        raise CyclesPreviewError("scene_path must end in lowercase .blend")
    return path


def _parse_payload(unit: SpecUnit) -> dict[str, Any]:
    if not isinstance(unit, SpecUnit) or unit.modality != MODALITY:
        raise CyclesPreviewError("Cycles backend requires a render SpecUnit")
    payload = unit.payload
    if not isinstance(payload, dict):
        raise CyclesPreviewError("Cycles render payload must be an object")
    expected = {
        "scene_path",
        "scene_sha256",
        "width",
        "height",
        "frame",
        "draft_samples",
        "verify_samples",
        "repair_samples",
    }
    if set(payload) != expected:
        raise CyclesPreviewError(
            "Cycles render payload must contain exactly " + ",".join(sorted(expected))
        )
    scene_path = _strict_relative_blend(payload["scene_path"])
    scene_sha = payload["scene_sha256"]
    if (
        not isinstance(scene_sha, str)
        or len(scene_sha) != 64
        or any(ch not in "0123456789abcdef" for ch in scene_sha)
    ):
        raise CyclesPreviewError(
            "scene_sha256 must be exactly 64 lowercase hexadecimal characters"
        )
    width = _bounded_int(payload["width"], "width", MIN_DIMENSION, MAX_DIMENSION)
    height = _bounded_int(payload["height"], "height", MIN_DIMENSION, MAX_DIMENSION)
    if width * height > MAX_PIXELS:
        raise CyclesPreviewError(f"width*height exceeds the {MAX_PIXELS}-pixel limit")
    frame = _bounded_int(payload["frame"], "frame", 0, MAX_FRAME)
    draft_samples = _bounded_int(
        payload["draft_samples"], "draft_samples", 1, MAX_LOW_SAMPLES
    )
    verify_samples = _bounded_int(
        payload["verify_samples"], "verify_samples", 1, MAX_LOW_SAMPLES
    )
    repair_samples = _bounded_int(
        payload["repair_samples"], "repair_samples", 2, MAX_HIGH_SAMPLES
    )
    if repair_samples <= max(draft_samples, verify_samples):
        raise CyclesPreviewError(
            "repair_samples must be greater than both independent low-SPP sample counts"
        )
    return {
        "scene_path": scene_path.as_posix(),
        "scene_sha256": scene_sha,
        "width": width,
        "height": height,
        "frame": frame,
        "draft_samples": draft_samples,
        "verify_samples": verify_samples,
        "repair_samples": repair_samples,
    }


def _resolve_scene(scene_root: Path, relative: str) -> Path:
    current = scene_root
    for component in PurePosixPath(relative).parts:
        current = current / component
        try:
            if current.is_symlink():
                raise CyclesPreviewError("scene_path cannot traverse symlinks")
        except OSError as exc:
            raise CyclesPreviewError(f"cannot inspect scene_path component: {exc}") from exc
    try:
        canonical = current.resolve(strict=True)
        canonical.relative_to(scene_root)
    except (OSError, ValueError) as exc:
        raise CyclesPreviewError("scene_path escapes or is absent from the scene root") from exc
    if not canonical.is_file():
        raise CyclesPreviewError("scene_path must name a regular .blend file")
    return canonical


def _bundle_digest(entries: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        relative = entry["path"].encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(entry["bytes"].to_bytes(8, "big"))
        digest.update(bytes.fromhex(entry["sha256"]))
    return digest.hexdigest()


def _copy_pinned_scene(
    source: Path, expected_sha: str, session: dict[str, Any]
) -> dict[str, Any]:
    """Snapshot the bounded operator scene root, not only its main .blend.

    Blender's ``//`` textures and linked libraries are relative to the scene
    bundle. Copying one .blend made real scenes render with missing magenta
    assets. The private snapshot is content-addressed and symlink-free. Its
    initial hashes are atomically bound to pre/post-render stat sentinels.
    """
    key = (str(source), expected_sha)
    cached = _SCENE_CACHE.get(key)
    if cached is not None:
        return cached

    scene_root = session["scene_root"]
    try:
        scene_relative = source.relative_to(scene_root)
    except ValueError as exc:
        raise CyclesPreviewError("scene escaped its operator root") from exc
    candidates: list[tuple[Path, Path]] = []
    for path in sorted(scene_root.rglob("*"), key=lambda value: value.as_posix()):
        if path.is_symlink():
            raise CyclesPreviewError("scene bundle cannot contain symlinks")
        if path.is_dir():
            continue
        if not path.is_file():
            raise CyclesPreviewError("scene bundle contains a non-regular entry")
        candidates.append((path, path.relative_to(scene_root)))
        if len(candidates) > MAX_SCENE_FILES:
            raise CyclesPreviewError(
                f"scene bundle exceeds the {MAX_SCENE_FILES}-file preview limit"
            )
    if not candidates:
        raise CyclesPreviewError("scene bundle is empty")

    temporary = session["scenes"] / f".bundle-{secrets.token_hex(16)}.tmp"
    temporary.mkdir(mode=0o700)
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    main_sha: str | None = None
    try:
        for original, relative in candidates:
            destination = temporary / relative
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            input_flags = os.O_RDONLY
            output_flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                input_flags |= os.O_NOFOLLOW
                output_flags |= os.O_NOFOLLOW
            source_fd = os.open(original, input_flags)
            try:
                output_fd = os.open(destination, output_flags, 0o600)
            except BaseException:
                os.close(source_fd)
                raise
            file_digest = hashlib.sha256()
            file_bytes = 0
            try:
                source_before = os.fstat(source_fd)
                if not stat.S_ISREG(source_before.st_mode):
                    raise CyclesPreviewError("scene bundle source changed type during copy")
                with (
                    os.fdopen(source_fd, "rb", closefd=False) as source_handle,
                    os.fdopen(output_fd, "wb", closefd=False) as output_handle,
                ):
                    while True:
                        chunk = source_handle.read(1 << 20)
                        if not chunk:
                            break
                        file_bytes += len(chunk)
                        total_bytes += len(chunk)
                        if total_bytes > MAX_SCENE_BYTES:
                            raise CyclesPreviewError(
                                f"scene bundle exceeds the {MAX_SCENE_BYTES}-byte preview limit"
                            )
                        file_digest.update(chunk)
                        output_handle.write(chunk)
                    output_handle.flush()
                    os.fsync(output_handle.fileno())
                source_after = os.fstat(source_fd)
                if _identity_from_stat(source_before) != _identity_from_stat(source_after):
                    raise CyclesPreviewError("scene bundle source changed during copy")
                output_before = os.fstat(output_fd)
                if not stat.S_ISREG(output_before.st_mode):
                    raise CyclesPreviewError("scene bundle destination changed type during copy")
                os.lseek(output_fd, 0, os.SEEK_SET)
                copied_digest = hashlib.sha256()
                copied_bytes = 0
                while True:
                    chunk = os.read(output_fd, 1 << 20)
                    if not chunk:
                        break
                    copied_bytes += len(chunk)
                    copied_digest.update(chunk)
                output_after = os.fstat(output_fd)
                output_identity = _identity_from_stat(output_after)
                if (
                    _identity_from_stat(output_before) != output_identity
                    or copied_bytes != file_bytes
                    or copied_digest.digest() != file_digest.digest()
                ):
                    raise CyclesPreviewError(
                        "private scene bundle copy changed while it was hashed"
                    )
            finally:
                os.close(source_fd)
                os.close(output_fd)
            if _regular_file_identity(destination) != output_identity:
                raise CyclesPreviewError(
                    "private scene bundle copy path changed after it was hashed"
                )
            file_sha = file_digest.hexdigest()
            relative_wire = relative.as_posix()
            entries.append(
                {
                    "path": relative_wire,
                    "bytes": file_bytes,
                    "sha256": file_sha,
                    "file_identity": output_identity,
                }
            )
            if relative == scene_relative:
                main_sha = file_sha
        if main_sha != expected_sha:
            raise CyclesPreviewError(
                f"scene_sha256 mismatch: expected {expected_sha}, got {main_sha}"
            )
        bundle_sha = _bundle_digest(entries)
        destination_root = session["scenes"] / f"bundle-{bundle_sha}-{expected_sha}"
        os.replace(temporary, destination_root)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    for entry in entries:
        copied = destination_root / Path(*PurePosixPath(entry["path"]).parts)
        if _regular_file_identity(copied) != entry["file_identity"]:
            raise CyclesPreviewError("private scene bundle changed during publication")
    bundle = {
        "root": destination_root,
        "root_identity": _directory_identity(destination_root),
        "scene": destination_root / scene_relative,
        "sha256": bundle_sha,
        "bytes": total_bytes,
        "files": len(entries),
        "entries": entries,
    }
    _SCENE_CACHE[key] = bundle
    return bundle


def _descriptor(context: dict[str, Any], phase: str) -> dict[str, Any]:
    manifest = context["unit_dir"] / f"{phase}-manifest.json"
    relative = manifest.relative_to(context["session"]["output_root"])
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "kind": ARTIFACT_KIND,
        "manifest_path": relative.as_posix(),
    }


def _context_for_unit(unit: SpecUnit) -> dict[str, Any]:
    payload = _parse_payload(unit)
    session = _get_session()
    private_benchmark_policy = (
        session["candidate_profile"]["name"] != "native"
        or session["resident_policy"] != RESIDENT_POLICY_BROAD
    )
    if private_benchmark_policy:
        if (
            unit.unit_id != BENCHMARK_PROFILE_UNIT_ID
            or set(unit.meta) != {BENCHMARK_PROFILE_META_KEY}
            or not isinstance(unit.meta[BENCHMARK_PROFILE_META_KEY], str)
            or not secrets.compare_digest(
                unit.meta[BENCHMARK_PROFILE_META_KEY],
                session["candidate_profile_auth"],
            )
        ):
            raise CyclesPreviewError(
                "private benchmark policy lacks the per-unit benchmark "
                "capability"
            )
    binding_bytes = _canonical_json(
        {
            "binding_policy": BINDING_POLICY,
            "modality": unit.modality,
            "operator_policy": {
                "candidate_profile": session["candidate_profile"],
                "candidate_denoising_policy": session[
                    "candidate_denoising_policy"
                ],
                "candidate_profile_scope": session["candidate_profile_scope"],
                "resident_policy": session["resident_policy"],
                "profile_authorization": (
                    "benchmark_capability_v1"
                    if private_benchmark_policy
                    else "native_only"
                ),
                "png_compression": PNG_COMPRESSION,
            },
            "payload": payload,
            "unit_id": unit.unit_id,
        }
    )
    binding = hashlib.sha256(binding_bytes).hexdigest()
    binding_key = f"{unit.unit_id}\x00{binding}"
    existing_token = _BINDINGS.get(binding_key)
    if existing_token is not None:
        context = _CONTEXTS[existing_token]
        if context["unit"] is not unit:
            raise CyclesPreviewError("duplicate unit binding used a different SpecUnit")
        return context

    source = _resolve_scene(session["scene_root"], payload["scene_path"])
    scene_bundle = _copy_pinned_scene(source, payload["scene_sha256"], session)
    scene_copy = scene_bundle["scene"]
    directory_digest = hashlib.sha256(binding_key.encode("utf-8")).hexdigest()
    unit_dir = session["units"] / f"unit-{directory_digest}"
    try:
        unit_dir.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise CyclesPreviewError("duplicate unit output directory in private session") from exc

    base_seed = int(binding[:8], 16) & 0x7FFFFFFF
    token = secrets.token_hex(32)
    context = {
        "token": token,
        "unit": unit,
        "binding": binding,
        "payload": payload,
        "session": session,
        "source_scene": source,
        "scene_bundle": scene_bundle,
        "scene_copy": scene_copy,
        "unit_dir": unit_dir,
        "seeds": {
            "draft": base_seed,
            "verify": base_seed ^ 0x5A5A5A5A,
            "repair": base_seed ^ 0x13579BDF,
            "baseline": base_seed ^ 0x2468ACE0,
        },
        "verified": False,
        "repaired": False,
    }
    context["descriptors"] = {
        phase: _descriptor(context, phase) for phase in ("draft", "repair", "baseline")
    }
    _CONTEXTS[token] = context
    _BINDINGS[binding_key] = token
    return context


def _assert_private_path(path: Path, session: dict[str, Any]) -> None:
    try:
        path.parent.resolve(strict=True).relative_to(session["root"])
    except (OSError, ValueError) as exc:
        raise CyclesPreviewError("render output path escaped the private session") from exc
    if path.parent.is_symlink():
        raise CyclesPreviewError("render output parent cannot be a symlink")


def _sanitized_blender_env(session: dict[str, Any]) -> dict[str, str]:
    return {
        # Never load operator/user Blender preferences, add-ons, startup files,
        # user-site Python, or loader-path overrides outside the pinned identity.
        "HOME": str(session["tmp"]),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "TMPDIR": str(session["tmp"]),
        # This value comes from validated operator configuration, never from a
        # buyer payload. The fixed child accepts only CPU or METAL.
        "CX_CYCLES_DEVICE": session["device"],
        "CX_CYCLES_CANDIDATE_PROFILE": session["candidate_profile"]["name"],
        "CX_CYCLES_RESIDENT_POLICY": session["resident_policy"],
    }


def _png_artifact_limit(width: int, height: int) -> int:
    if (
        type(width) is not int
        or type(height) is not int
        or not 1 <= width <= MAX_DIMENSION
        or not 1 <= height <= MAX_DIMENSION
        or width * height > MAX_PIXELS
    ):
        raise CyclesPreviewError("Cycles PNG dimensions are outside the preview bound")
    return min(MAX_ARTIFACT_BYTES, width * height * 8 + (1 << 20))


def _read_png_snapshot(
    path: Path, *, maximum: int
) -> tuple[bytes, tuple[int, ...]]:
    """Read one no-follow descriptor and bind bytes to its stable identity."""

    path = Path(path).absolute()
    try:
        initial = path.lstat()
    except OSError as exc:
        raise CyclesPreviewError(f"Cycles did not produce its PNG: {exc}") from exc
    if not stat.S_ISREG(initial.st_mode) or path.is_symlink():
        raise CyclesPreviewError("Cycles output must be a non-symlink regular PNG")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CyclesPreviewError(f"cannot open Cycles PNG snapshot: {exc}") from exc
    chunks: list[bytes] = []
    total = 0
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise CyclesPreviewError("Cycles output must be a regular PNG")
        if _identity_from_stat(initial) != _identity_from_stat(before):
            raise CyclesPreviewError("Cycles PNG path changed before snapshot read")
        if not 1 <= before.st_size <= maximum:
            raise CyclesPreviewError(
                f"Cycles PNG size must be in [1,{maximum}] bytes"
            )
        while True:
            chunk = os.read(fd, min(1 << 20, maximum - total + 1))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise CyclesPreviewError(
                    f"Cycles PNG size must be in [1,{maximum}] bytes"
                )
        after = os.fstat(fd)
        identity = _identity_from_stat(after)
        if (
            _identity_from_stat(before) != identity
            or total != after.st_size
        ):
            raise CyclesPreviewError("Cycles PNG changed during snapshot read")
    finally:
        os.close(fd)
    if _regular_file_identity(path) != identity:
        raise CyclesPreviewError("Cycles PNG path changed after snapshot read")
    return b"".join(chunks), identity


def _validated_png_snapshot(
    path: Path,
    *,
    width: int,
    height: int,
    retain_pixels: bool,
) -> _ValidatedPngSnapshot:
    artifact_limit = _png_artifact_limit(width, height)
    absolute_path = Path(path).absolute()
    data, identity = _read_png_snapshot(absolute_path, maximum=artifact_limit)
    mode: str
    pixels: bytes | None = None
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG":
                raise CyclesPreviewError("Cycles output is not a PNG")
            if image.size != (width, height):
                raise CyclesPreviewError(
                    f"Cycles PNG is {image.size}, expected {(width, height)}"
                )
            if getattr(image, "n_frames", 1) != 1:
                raise CyclesPreviewError("Cycles PNG must contain exactly one frame")
            if image.mode not in {"RGB", "RGBA"}:
                raise CyclesPreviewError("Cycles PNG mode must be RGB or RGBA")
            mode = image.mode
            image.load()
            if retain_pixels:
                pixels = image.tobytes()
    except CyclesPreviewError:
        raise
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise CyclesPreviewError(f"cannot decode Cycles PNG: {exc}") from exc
    if pixels is not None:
        channels = 3 if mode == "RGB" else 4
        if len(pixels) != width * height * channels:
            raise CyclesPreviewError("decoded Cycles PNG byte count mismatch")
    return _ValidatedPngSnapshot(
        path=absolute_path,
        width=width,
        height=height,
        mode=mode,
        source_bytes=data,
        pixel_bytes=pixels,
        sha256=hashlib.sha256(data).hexdigest(),
        file_identity=identity,
    )


def _validate_png(path: Path, *, width: int, height: int) -> str:
    """Validate and hash one immutable PNG snapshot, preserving the SHA API."""

    return _validated_png_snapshot(
        path, width=width, height=height, retain_pixels=False
    ).sha256


def _is_lower_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _retained_png_binding_sha256(record: RetainedValidatedPng) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "context_binding_sha256": record.context_binding_sha256,
                "file_identity": list(record.file_identity),
                "height": record.height,
                "mode": record.mode,
                "path": str(record.path),
                "phase": record.phase,
                "pixel_bytes": len(record.pixel_bytes),
                "pixel_sha256": record.pixel_sha256,
                "source_bytes": len(record.source_bytes),
                "source_sha256": record.sha256,
                "width": record.width,
            }
        )
    ).hexdigest()


def _discard_retained_validated_pngs(context: dict[str, Any]) -> None:
    """Drop all retained byte capabilities without trusting their shape."""

    cached = context.pop(_RETAINED_VALIDATED_PNGS_KEY, None)
    if isinstance(cached, dict):
        cached.clear()


def _cache_retained_validated_png(
    context: dict[str, Any], phase: str, snapshot: _ValidatedPngSnapshot
) -> RetainedValidatedPng:
    """Bind one successful validation to its exact request and render phase."""

    try:
        if phase not in _RETAINABLE_PNG_PHASES:
            raise CyclesPreviewError(
                "retained PNG validation is limited to draft and verify"
            )
        context_binding = context.get("binding")
        if not _is_lower_sha256(context_binding):
            raise CyclesPreviewError("retained PNG context binding is invalid")
        if type(snapshot) is not _ValidatedPngSnapshot or snapshot.pixel_bytes is None:
            raise CyclesPreviewError("retained PNG snapshot lacks decoded pixels")
        cache = context.setdefault(_RETAINED_VALIDATED_PNGS_KEY, {})
        if type(cache) is not dict:
            raise CyclesPreviewError("retained PNG cache shape mismatch")
        if len(cache) >= _MAX_RETAINED_VALIDATED_PNGS or phase in cache:
            raise CyclesPreviewError("retained PNG cache replacement rejected")
        pixel_sha = hashlib.sha256(snapshot.pixel_bytes).hexdigest()
        record = RetainedValidatedPng(
            phase=phase,
            path=snapshot.path,
            width=snapshot.width,
            height=snapshot.height,
            mode=snapshot.mode,
            source_bytes=snapshot.source_bytes,
            pixel_bytes=snapshot.pixel_bytes,
            sha256=snapshot.sha256,
            pixel_sha256=pixel_sha,
            file_identity=snapshot.file_identity,
            context_binding_sha256=context_binding,
            binding_sha256="",
        )
        record = record._replace(
            binding_sha256=_retained_png_binding_sha256(record)
        )
        cache[phase] = record
        return record
    except BaseException:
        _discard_retained_validated_pngs(context)
        raise


def _pop_retained_validated_png(
    context: dict[str, Any],
    *,
    phase: str,
    path: Path,
    sha256: str,
) -> RetainedValidatedPng:
    """Consume exactly one bound retained decode, discarding on any mismatch."""

    cache = context.get(_RETAINED_VALIDATED_PNGS_KEY)
    if type(cache) is not dict or not 1 <= len(cache) <= _MAX_RETAINED_VALIDATED_PNGS:
        _discard_retained_validated_pngs(context)
        raise CyclesPreviewError("retained PNG cache is unavailable or malformed")
    record = cache.pop(phase, None)
    if not cache:
        context.pop(_RETAINED_VALIDATED_PNGS_KEY, None)
    try:
        expected_path = Path(path).absolute()
        context_binding = context.get("binding")
        if (
            type(record) is not RetainedValidatedPng
            or phase not in _RETAINABLE_PNG_PHASES
            or record.phase != phase
            or record.path != expected_path
            or record.sha256 != sha256
            or not _is_lower_sha256(sha256)
            or record.context_binding_sha256 != context_binding
            or not _is_lower_sha256(record.context_binding_sha256)
            or record.mode not in {"RGB", "RGBA"}
            or type(record.source_bytes) is not bytes
            or type(record.pixel_bytes) is not bytes
            or hashlib.sha256(record.source_bytes).hexdigest() != record.sha256
            or hashlib.sha256(record.pixel_bytes).hexdigest()
            != record.pixel_sha256
            or record.binding_sha256 != _retained_png_binding_sha256(record)
            or len(record.source_bytes)
            > _png_artifact_limit(record.width, record.height)
            or len(record.source_bytes) < 1
            or len(record.pixel_bytes)
            != record.width
            * record.height
            * (3 if record.mode == "RGB" else 4)
            or _regular_file_identity(expected_path) != record.file_identity
        ):
            raise CyclesPreviewError("retained PNG identity mismatch")
        return record
    except BaseException:
        _discard_retained_validated_pngs(context)
        raise


def _worker_key(context: dict[str, Any]) -> tuple[str, ...]:
    session = context["session"]
    return (
        str(context["scene_copy"]),
        context["payload"]["scene_sha256"],
        context["scene_bundle"]["sha256"],
        session["blender_sha256"],
        _BLENDER_CHILD_SHA256,
        _canonical_json(session["candidate_profile"]).decode("ascii"),
        _canonical_json(session["candidate_denoising_policy"]).decode("ascii"),
        session["resident_policy"],
        session["device"],
    )


def _close_fd(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _kill_worker_process(worker: dict[str, Any]) -> None:
    process = worker["process"]
    if worker.get("owns_process_group") and os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    try:
        process.kill()
    except OSError:
        pass


def _close_worker(worker: dict[str, Any], *, graceful: bool) -> None:
    """Close one resident renderer without ever signaling the outer group."""
    command_fd = worker.pop("command_fd", None)
    response_fd = worker.pop("response_fd", None)
    _close_fd(command_fd)
    process = worker["process"]
    if graceful and process.poll() is None:
        try:
            process.wait(timeout=min(float(worker["timeout"]), 5.0))
        except (subprocess.TimeoutExpired, OSError):
            _kill_worker_process(worker)
    elif process.poll() is None:
        _kill_worker_process(worker)
    try:
        process.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        if process.poll() is None:
            _kill_worker_process(worker)
            try:
                process.wait(timeout=1)
            except (subprocess.TimeoutExpired, OSError):
                pass
    _close_fd(response_fd)


def _shutdown_worker() -> None:
    global _WORKER
    worker, _WORKER = _WORKER, None
    if worker is not None:
        try:
            _close_worker(worker, graceful=True)
        except BaseException:
            # This is also an atexit hook. Cleanup must never turn a completed,
            # already-validated preview envelope into corrupt stdout.
            pass


def _quarantine_worker(worker: dict[str, Any]) -> None:
    global _WORKER
    if _WORKER is worker:
        _WORKER = None
    _close_worker(worker, graceful=False)


def _deadline_remaining(
    worker: dict[str, Any], deadline: float, phase: str
) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CyclesPreviewError(
            f"Cycles {phase} render exceeded {worker['timeout']} seconds"
        )
    return remaining


def _write_worker_frame(
    worker: dict[str, Any], encoded: bytes, deadline: float, phase: str
) -> None:
    if not encoded.endswith(b"\n") or len(encoded) > MAX_WORKER_FRAME_BYTES:
        raise CyclesPreviewError("Cycles worker command exceeded its fixed bound")
    offset = 0
    fd = worker["command_fd"]
    while offset < len(encoded):
        remaining = _deadline_remaining(worker, deadline, phase)
        try:
            _, writable, _ = select.select([], [fd], [], remaining)
        except (OSError, ValueError) as exc:
            raise CyclesPreviewError(f"waiting to write Cycles worker command: {exc}") from exc
        if not writable:
            raise CyclesPreviewError(
                f"Cycles {phase} render exceeded {worker['timeout']} seconds"
            )
        try:
            written = os.write(fd, encoded[offset:])
        except (BrokenPipeError, OSError) as exc:
            raise CyclesPreviewError(f"Cycles worker command pipe failed: {exc}") from exc
        if written <= 0:
            raise CyclesPreviewError("Cycles worker command pipe made no progress")
        offset += written


def _read_worker_frame(
    worker: dict[str, Any], deadline: float, phase: str
) -> dict[str, Any]:
    buffer: bytearray = worker["response_buffer"]
    fd = worker["response_fd"]
    while True:
        newline = buffer.find(b"\n")
        if newline >= 0:
            if newline + 1 > MAX_WORKER_FRAME_BYTES:
                raise CyclesPreviewError("Cycles worker response exceeded its fixed bound")
            raw = bytes(buffer[:newline])
            del buffer[: newline + 1]
            if not raw:
                raise CyclesPreviewError("Cycles worker returned an empty protocol frame")
            try:
                value = json.loads(
                    raw.decode("utf-8"),
                    object_pairs_hook=_reject_duplicate_object,
                    parse_constant=_reject_json_constant,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise CyclesPreviewError(
                    "Cycles worker returned malformed strict JSON"
                ) from exc
            if not isinstance(value, dict):
                raise CyclesPreviewError("Cycles worker protocol frame must be an object")
            return value
        if len(buffer) > MAX_WORKER_FRAME_BYTES:
            raise CyclesPreviewError("Cycles worker response exceeded its fixed bound")
        remaining = _deadline_remaining(worker, deadline, phase)
        try:
            readable, _, _ = select.select([fd], [], [], remaining)
        except (OSError, ValueError) as exc:
            raise CyclesPreviewError(f"waiting for Cycles worker response: {exc}") from exc
        if not readable:
            raise CyclesPreviewError(
                f"Cycles {phase} render exceeded {worker['timeout']} seconds"
            )
        try:
            chunk = os.read(fd, 4096)
        except OSError as exc:
            raise CyclesPreviewError(f"reading Cycles worker response: {exc}") from exc
        if not chunk:
            return_code = worker["process"].poll()
            raise CyclesPreviewError(
                f"resident Blender exited {return_code!r} before the {phase} response"
            )
        buffer.extend(chunk)


def _start_worker(
    context: dict[str, Any], deadline: float, phase: str
) -> dict[str, Any]:
    if os.name != "posix":
        raise CyclesPreviewError("resident Cycles preview worker requires POSIX pipes")
    session = context["session"]
    command_read, command_write = os.pipe()
    response_read, response_write = os.pipe()
    nonce = secrets.token_hex(32)
    command = (
        str(session["blender"]),
        "--background",
        "--factory-startup",
        "--disable-autoexec",
        str(context["scene_copy"]),
        "--python",
        str(session["child_script"]),
        "--",
        str(command_read),
        str(response_write),
        nonce,
        str(session["root"]),
    )
    try:
        group_options = (
            {"start_new_session": True} if session["local_process_group"] else {}
        )
        process = subprocess.Popen(  # noqa: S603 - all executable/code fields are pinned
            command,
            cwd=session["root"],
            env=_sanitized_blender_env(session),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(command_read, response_write),
            # The Rust preview driver owns one process group for its entire
            # subprocess tree. Blender inherits it so the outer request timeout
            # remains the authoritative whole-tree kill boundary.
            **group_options,
        )
    except OSError as exc:
        for fd in (command_read, command_write, response_read, response_write):
            _close_fd(fd)
        raise CyclesPreviewError(f"could not start pinned Blender worker: {exc}") from exc
    _close_fd(command_read)
    _close_fd(response_write)
    worker = {
        "process": process,
        "command_fd": command_write,
        "response_fd": response_read,
        "response_buffer": bytearray(),
        "commands": 0,
        "key": _worker_key(context),
        "timeout": session["timeout"],
        "owns_process_group": session["local_process_group"],
    }
    try:
        ready = _read_worker_frame(worker, deadline, phase)
        if set(ready) != {
            "blender_build_hash",
            "blender_version",
            "candidate_denoising_policy",
            "candidate_profile",
            "dependency_count",
            "dependency_paths_sha256",
            "device",
            "enabled_device_names",
            "kind",
            "native_integrator",
            "nonce",
            "png_compression",
            "protocol",
            "reference_denoising_policy",
            "reference_sampling",
            "resident_policy",
            "scene_path",
            "schema_version",
        }:
            raise CyclesPreviewError("Cycles worker handshake shape mismatch")
        expected_device = "GPU/METAL" if session["device"] == "METAL" else "CPU"
        enabled_names = ready["enabled_device_names"]
        native_integrator = ready["native_integrator"]
        reference_sampling = ready["reference_sampling"]
        reference_denoising_policy = ready["reference_denoising_policy"]
        if (
            not isinstance(ready["schema_version"], int)
            or isinstance(ready["schema_version"], bool)
            or ready["schema_version"] != 1
            or not isinstance(ready["kind"], str)
            or ready["kind"] != "ready"
            or not isinstance(ready["protocol"], str)
            or ready["protocol"] != WORKER_PROTOCOL
            or not isinstance(ready["nonce"], str)
            or ready["nonce"] != nonce
            or not isinstance(ready["scene_path"], str)
            or ready["scene_path"] != str(context["scene_copy"])
            or not isinstance(ready["device"], str)
            or ready["device"] != expected_device
            or not isinstance(ready["blender_version"], str)
            or not 1 <= len(ready["blender_version"]) <= 128
            or not isinstance(ready["blender_build_hash"], str)
            or not 1 <= len(ready["blender_build_hash"]) <= 128
            or ready["candidate_profile"] != session["candidate_profile"]
            or ready["candidate_denoising_policy"]
            != session["candidate_denoising_policy"]
            or ready["resident_policy"] != session["resident_policy"]
            or reference_denoising_policy != DENOISING_OFF_POLICY
            or not isinstance(native_integrator, dict)
            or set(native_integrator) != {
                "max_bounces",
                "diffuse_bounces",
                "glossy_bounces",
                "transmission_bounces",
            }
            or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 <= value <= 128
                for value in native_integrator.values()
            )
            or not isinstance(reference_sampling, dict)
            or set(reference_sampling) != {
                "use_light_tree",
                "use_adaptive_sampling",
                "adaptive_min_samples",
                "adaptive_threshold",
            }
            or type(reference_sampling.get("use_light_tree")) is not bool
            or reference_sampling.get("use_adaptive_sampling") is not False
            or not isinstance(reference_sampling.get("adaptive_min_samples"), int)
            or isinstance(reference_sampling.get("adaptive_min_samples"), bool)
            or not 0 <= reference_sampling["adaptive_min_samples"] <= 4096
            or isinstance(reference_sampling.get("adaptive_threshold"), bool)
            or not isinstance(reference_sampling.get("adaptive_threshold"), (int, float))
            or not 0.0 <= float(reference_sampling["adaptive_threshold"]) <= 1.0
            or not isinstance(ready["dependency_count"], int)
            or isinstance(ready["dependency_count"], bool)
            or not 0 <= ready["dependency_count"] <= MAX_SCENE_FILES
            or not isinstance(ready["dependency_paths_sha256"], str)
            or len(ready["dependency_paths_sha256"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in ready["dependency_paths_sha256"]
            )
            or not isinstance(ready["png_compression"], int)
            or isinstance(ready["png_compression"], bool)
            or ready["png_compression"] != PNG_COMPRESSION
            or not isinstance(enabled_names, list)
            or not 1 <= len(enabled_names) <= 16
            or any(
                not isinstance(name, str) or not 1 <= len(name) <= 256
                for name in enabled_names
            )
        ):
            raise CyclesPreviewError("Cycles worker handshake identity mismatch")
        worker_identity = {
            key: ready[key]
            for key in (
                "blender_build_hash",
                "blender_version",
                "candidate_denoising_policy",
                "candidate_profile",
                "dependency_count",
                "dependency_paths_sha256",
                "device",
                "enabled_device_names",
                "native_integrator",
                "png_compression",
                "reference_denoising_policy",
                "reference_sampling",
                "resident_policy",
            )
        }
        worker["renderer_identity"] = worker_identity
        context["worker_renderer_identity"] = worker_identity
    except BaseException:
        _close_worker(worker, graceful=False)
        raise
    return worker


def _worker_for_context(
    context: dict[str, Any], deadline: float, phase: str
) -> dict[str, Any]:
    global _WORKER
    expected_key = _worker_key(context)
    worker = _WORKER
    if worker is not None and (
        worker["process"].poll() is not None
        or worker["key"] != expected_key
        or worker["commands"] >= MAX_WORKER_COMMANDS
    ):
        _WORKER = None
        _close_worker(worker, graceful=worker["process"].poll() is None)
        worker = None
    if worker is None:
        worker = _start_worker(context, deadline, phase)
        _WORKER = worker
    # A resident worker can serve a later frame/context with the same pinned
    # scene and device. Propagate its attested handshake into every manifest,
    # not only the context that originally spawned it.
    context["worker_renderer_identity"] = worker["renderer_identity"]
    return worker


def _revalidate_execution_identity(context: dict[str, Any]) -> None:
    session = context["session"]
    if _operator_snapshot() != session["operator_snapshot"]:
        raise CyclesPreviewError("operator Cycles configuration changed during a request")
    if _regular_file_identity(session["blender"]) != session["blender_file_identity"]:
        raise CyclesPreviewError("operator-pinned Blender executable changed before execution")
    if (
        _regular_file_identity(session["child_script"])
        != session["child_script_file_identity"]
    ):
        raise CyclesPreviewError("fixed Cycles child script changed before execution")
    bundle = context["scene_bundle"]
    if _directory_identity(bundle["root"]) != bundle["root_identity"]:
        raise CyclesPreviewError("private pinned scene bundle root changed before execution")
    expected_paths = {entry["path"] for entry in bundle["entries"]}
    observed_paths: set[str] = set()
    for candidate in bundle["root"].rglob("*"):
        if candidate.is_symlink():
            raise CyclesPreviewError("private pinned scene bundle gained a symlink")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise CyclesPreviewError("private pinned scene bundle gained a special entry")
        observed_paths.add(candidate.relative_to(bundle["root"]).as_posix())
    if observed_paths != expected_paths:
        raise CyclesPreviewError("private pinned scene bundle file set changed before execution")
    for expected in bundle["entries"]:
        candidate = bundle["root"] / Path(*PurePosixPath(expected["path"]).parts)
        if _regular_file_identity(candidate) != expected["file_identity"]:
            if candidate == context["scene_copy"]:
                raise CyclesPreviewError(
                    "private pinned scene copy changed before execution"
                )
            raise CyclesPreviewError("private pinned scene bundle changed before execution")


def _invoke_blender(
    context: dict[str, Any],
    phase: str,
    samples: int,
    seed: int,
    output: Path,
    *,
    border: tuple[int, int, int, int] | None = None,
    execution_label: str | None = None,
    retain_validated_png: bool = False,
) -> str:
    if type(retain_validated_png) is not bool:
        raise CyclesPreviewError("retain_validated_png must be a boolean")
    if retain_validated_png and phase not in _RETAINABLE_PNG_PHASES:
        _discard_retained_validated_pngs(context)
        raise CyclesPreviewError(
            "retained PNG validation is limited to draft and verify"
        )
    session = context["session"]
    try:
        _assert_private_path(output, session)
        if output.exists() or output.is_symlink():
            raise CyclesPreviewError(f"refusing to overwrite existing {phase} output")
    except BaseException:
        if retain_validated_png:
            _discard_retained_validated_pngs(context)
        raise

    # Preserve the original per-phase content checks even when the already
    # loaded process is reused. Pin changes quarantine the request immediately.
    try:
        _revalidate_execution_identity(context)
    except BaseException:
        if retain_validated_png:
            _discard_retained_validated_pngs(context)
        _shutdown_worker()
        raise

    payload = context["payload"]
    if border is not None:
        if (
            not isinstance(border, tuple)
            or len(border) != 4
            or any(not isinstance(value, int) or isinstance(value, bool) for value in border)
        ):
            if retain_validated_png:
                _discard_retained_validated_pngs(context)
            raise CyclesPreviewError("internal Cycles border shape mismatch")
        left, top, right, bottom = border
        if not (
            0 <= left < right <= payload["width"]
            and 0 <= top < bottom <= payload["height"]
        ):
            if retain_validated_png:
                _discard_retained_validated_pngs(context)
            raise CyclesPreviewError("internal Cycles border is outside the render")
    label = phase if execution_label is None else execution_label
    if (
        not isinstance(label, str)
        or not 1 <= len(label) <= 64
        or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for ch in label)
    ):
        if retain_validated_png:
            _discard_retained_validated_pngs(context)
        raise CyclesPreviewError("internal Cycles execution label is invalid")
    config = {
        "border": list(border) if border is not None else None,
        "candidate_denoising_policy": session["candidate_denoising_policy"],
        "candidate_profile": session["candidate_profile"],
        "frame": payload["frame"],
        "height": payload["height"],
        "output": str(output),
        "phase": phase,
        "resident_policy": session["resident_policy"],
        # The independent verifier skips the sample range used by its paired
        # draft. This is derived by trusted backend code, never supplied by the
        # buyer request.
        "sample_offset": (
            payload["draft_samples"]
            if phase == "verify"
            else (
                payload["draft_samples"] + payload["verify_samples"]
                if phase == "baseline"
                else 0
            )
        ),
        "samples": samples,
        "seed": seed,
        "width": payload["width"],
    }
    config_path = context["unit_dir"] / f"{label}-render-config.json"
    try:
        _write_new_file(config_path, _canonical_json(config) + b"\n")
        deadline = time.monotonic() + session["timeout"]
        worker = _worker_for_context(context, deadline, phase)
    except BaseException:
        if retain_validated_png:
            _discard_retained_validated_pngs(context)
        raise
    command_id = worker["commands"] + 1
    command = {"command_id": command_id, **config}
    command_sha = hashlib.sha256(_canonical_json(command)).hexdigest()
    frame = {
        "schema_version": 1,
        "kind": "render",
        "command": command,
        "command_sha256": command_sha,
    }
    encoded = _canonical_json(frame) + b"\n"
    try:
        _write_worker_frame(worker, encoded, deadline, phase)
        response = _read_worker_frame(worker, deadline, phase)
        success_keys = {
            "command_id",
            "command_sha256",
            "denoising",
            "integrator",
            "kind",
            "ok",
            "resident_mutation",
            "resident_policy",
            "sampling",
            "schema_version",
        }
        error_keys = (
            success_keys - {"denoising", "integrator", "sampling"}
        ) | {"error"}
        response_keys = frozenset(response)
        if response_keys not in {frozenset(success_keys), frozenset(error_keys)}:
            raise CyclesPreviewError("Cycles worker response shape mismatch")
        if (
            not isinstance(response["schema_version"], int)
            or isinstance(response["schema_version"], bool)
            or response["schema_version"] != 1
            or not isinstance(response["kind"], str)
            or response["kind"] != "render_result"
            or not isinstance(response["command_id"], int)
            or isinstance(response["command_id"], bool)
            or response["command_id"] != command_id
            or not isinstance(response["command_sha256"], str)
            or len(response["command_sha256"]) != 64
            or response["command_sha256"] != command_sha
            or not isinstance(response["ok"], bool)
            or response["resident_policy"] != session["resident_policy"]
        ):
            raise CyclesPreviewError("Cycles worker response identity mismatch")
        if response["ok"]:
            if set(response) != success_keys:
                raise CyclesPreviewError("successful Cycles worker response carried extra fields")
            actual_integrator = response["integrator"]
            expected_resident_mutation = (
                RESIDENT_POLICY_SAME_FRAME_MINIMAL
                if session["resident_policy"]
                == RESIDENT_POLICY_SAME_FRAME_MINIMAL
                and worker.get("last_successful_frame") == payload["frame"]
                else RESIDENT_POLICY_BROAD
            )
            if response["resident_mutation"] != expected_resident_mutation:
                raise CyclesPreviewError(
                    "Cycles worker resident mutation attestation mismatch"
                )
            if (
                not isinstance(actual_integrator, dict)
                or set(actual_integrator) != {
                    "max_bounces",
                    "diffuse_bounces",
                    "glossy_bounces",
                    "transmission_bounces",
                }
                or any(
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or not 0 <= value <= 128
                    for value in actual_integrator.values()
                )
            ):
                raise CyclesPreviewError("Cycles worker integrator report mismatch")
            native_integrator = worker["renderer_identity"]["native_integrator"]
            expected_integrator = dict(native_integrator)
            if phase in {"draft", "verify"}:
                expected_integrator = {
                    key: min(native_integrator[key], session["candidate_profile"][key])
                    if session["candidate_profile"][key] is not None
                    else native_integrator[key]
                    for key in native_integrator
                }
            if actual_integrator != expected_integrator:
                raise CyclesPreviewError(
                    "Cycles worker did not apply the pinned integrator profile"
                )
            actual_sampling = response["sampling"]
            if (
                not isinstance(actual_sampling, dict)
                or set(actual_sampling) != {
                    "use_light_tree",
                    "use_adaptive_sampling",
                    "adaptive_min_samples",
                    "adaptive_threshold",
                }
                or type(actual_sampling.get("use_light_tree")) is not bool
                or type(actual_sampling.get("use_adaptive_sampling")) is not bool
                or not isinstance(actual_sampling.get("adaptive_min_samples"), int)
                or isinstance(actual_sampling.get("adaptive_min_samples"), bool)
                or not 0 <= actual_sampling["adaptive_min_samples"] <= 4096
                or isinstance(actual_sampling.get("adaptive_threshold"), bool)
                or not isinstance(actual_sampling.get("adaptive_threshold"), (int, float))
                or not 0.0 <= float(actual_sampling["adaptive_threshold"]) <= 1.0
            ):
                raise CyclesPreviewError("Cycles worker sampling report mismatch")
            expected_sampling = dict(
                worker["renderer_identity"]["reference_sampling"]
            )
            if phase in {"draft", "verify"}:
                profile = session["candidate_profile"]
                if profile["use_light_tree"] is not None:
                    expected_sampling["use_light_tree"] = profile["use_light_tree"]
                if profile["use_adaptive_sampling"]:
                    expected_sampling.update(
                        {
                            "use_adaptive_sampling": True,
                            "adaptive_min_samples": profile["adaptive_min_samples"],
                            "adaptive_threshold": struct.unpack(
                                "f", struct.pack("f", profile["adaptive_threshold"])
                            )[0],
                        }
                    )
            if actual_sampling != expected_sampling:
                raise CyclesPreviewError(
                    "Cycles worker did not apply the pinned sampling profile"
                )
            actual_denoising = response["denoising"]
            expected_denoising = (
                session["candidate_denoising_policy"]
                if phase in {"draft", "verify"}
                else worker["renderer_identity"]["reference_denoising_policy"]
            )
            if actual_denoising != expected_denoising:
                raise CyclesPreviewError(
                    "Cycles worker did not apply the pinned denoising profile"
                )
            previous_integrator = context.setdefault(
                "actual_integrators", {}
            ).setdefault(phase, actual_integrator)
            if previous_integrator != actual_integrator:
                raise CyclesPreviewError(
                    "Cycles worker integrator changed across repeated phase commands"
                )
            previous_sampling = context.setdefault(
                "actual_sampling", {}
            ).setdefault(phase, actual_sampling)
            if previous_sampling != actual_sampling:
                raise CyclesPreviewError(
                    "Cycles worker sampling changed across repeated phase commands"
                )
            previous_denoising = context.setdefault(
                "actual_denoising", {}
            ).setdefault(phase, actual_denoising)
            if previous_denoising != actual_denoising:
                raise CyclesPreviewError(
                    "Cycles worker denoising changed across repeated phase commands"
                )
            context.setdefault("resident_mutation_history", []).append(
                {
                    "command_id": command_id,
                    "frame": payload["frame"],
                    "mutation": response["resident_mutation"],
                    "phase": phase,
                }
            )
            context.setdefault("actual_resident_mutation", {})[phase] = response[
                "resident_mutation"
            ]
            worker["last_successful_frame"] = payload["frame"]
        else:
            error = response.get("error")
            if not isinstance(error, str) or not error or len(error) > 500:
                raise CyclesPreviewError("Cycles worker error response was malformed")
            raise CyclesPreviewError(f"resident Blender rejected {phase}: {error}")
        worker["commands"] = command_id
    except BaseException:
        if retain_validated_png:
            _discard_retained_validated_pngs(context)
        _quarantine_worker(worker)
        raise
    try:
        # Catch changes that race the pre-render identity check before accepting
        # any artifact. Initial contents were SHA-256 hashed; these paired stat
        # sentinels avoid charging every render for ~268 MiB of repeat hashing.
        _revalidate_execution_identity(context)
        if not retain_validated_png:
            return _validate_png(
                output, width=payload["width"], height=payload["height"]
            )
        snapshot = _validated_png_snapshot(
            output,
            width=payload["width"],
            height=payload["height"],
            retain_pixels=True,
        )
        _cache_retained_validated_png(context, phase, snapshot)
        return snapshot.sha256
    except BaseException:
        if retain_validated_png:
            _discard_retained_validated_pngs(context)
        _quarantine_worker(worker)
        raise


def _artifact_manifest(
    context: dict[str, Any], phase: str, artifact: Path, artifact_sha: str,
    *,
    samples: int,
    seed: int,
    repair_strategy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    session = context["session"]
    payload = context["payload"]
    manifest = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "preview_only": True,
        "billing_eligible": False,
        "production_ready": False,
        "artifact_verified": False,
        "evidence": "synthetic",
        "execution_identity_revalidation": {
            "initial_content": "sha256",
            "per_render": "pre_and_post_stat_identity_plus_bundle_file_set",
        },
        "unit_id": context["unit"].unit_id,
        "binding_sha256": context["binding"],
        "phase": phase,
        "scene": {
            "relative_path": payload["scene_path"],
            "sha256": payload["scene_sha256"],
            "bundle_sha256": context["scene_bundle"]["sha256"],
            "bundle_files": context["scene_bundle"]["files"],
            "bundle_bytes": context["scene_bundle"]["bytes"],
        },
        "render": {
            "engine": "CYCLES",
            "device": (
                "GPU/METAL" if session["device"] == "METAL" else "CPU"
            ),
            "width": payload["width"],
            "height": payload["height"],
            "frame": payload["frame"],
            "samples": samples,
            "sample_offset": (
                payload["draft_samples"]
                if phase == "verify"
                else (
                    payload["draft_samples"] + payload["verify_samples"]
                    if phase == "baseline"
                    else 0
                )
            ),
            "seed": seed,
            "integrator_policy": {
                "mode": (
                    "candidate_capped"
                    if phase in {"draft", "verify"}
                    and session["candidate_profile"]["name"]
                    not in {"native", "oidn_native_v1"}
                    else "fixed_reference"
                ),
                "candidate_profile": session["candidate_profile"],
                "candidate_profile_scope": session["candidate_profile_scope"],
                "actual_integrator": context.get("actual_integrators", {}).get(phase),
                "actual_sampling": context.get("actual_sampling", {}).get(phase),
                "samples_are_cap_when_adaptive": bool(
                    context.get("actual_sampling", {})
                    .get(phase, {})
                    .get("use_adaptive_sampling", False)
                ),
                "repair_and_baseline_use_reference_policy": True,
            },
            "denoising_policy": {
                "mode": (
                    "candidate_oidn"
                    if phase in {"draft", "verify"}
                    and session["candidate_denoising_policy"]["use_denoising"]
                    else "fixed_off_reference"
                ),
                "candidate_policy": session["candidate_denoising_policy"],
                "actual": context.get("actual_denoising", {}).get(phase),
                "repair_and_baseline_denoising_disabled": True,
            },
            "pixel_filter": {
                "type": PIXEL_FILTER_TYPE,
                "width": PIXEL_FILTER_WIDTH,
            },
            "png_compression": PNG_COMPRESSION,
            "resident_policy": session["resident_policy"],
            "resident_mutation": context.get(
                "actual_resident_mutation", {}
            ).get(phase),
            "worker_renderer_identity": context.get("worker_renderer_identity"),
        },
        "artifact": {
            "path": artifact.relative_to(session["output_root"]).as_posix(),
            "sha256": artifact_sha,
            "media_type": "image/png",
        },
        "pins": {
            "blender_sha256": session["blender_sha256"],
            "backend_sha256": session["backend_sha256"],
            "child_script_sha256": _BLENDER_CHILD_SHA256,
            "controller_core_sha256": session["controller_core_sha256"],
            "controller_adapter_sha256": session["controller_adapter_sha256"],
        },
    }
    if repair_strategy is not None:
        manifest["repair_strategy"] = repair_strategy
    return manifest


def _load_rgb(path: Path, expected_size: tuple[int, int]) -> Image.Image:
    try:
        with Image.open(path) as source:
            if source.format != "PNG" or source.size != expected_size:
                raise CyclesPreviewError("agreement input PNG shape/format mismatch")
            # Alpha is deterministically opaque for these Cycles PNGs. Including
            # that unchanged fourth channel would give every comparison a free
            # 25% agreement floor and make the published threshold misleading.
            image = source.convert("RGB")
            image.load()
            return image
    except (OSError, UnidentifiedImageError) as exc:
        raise CyclesPreviewError(f"cannot decode agreement input: {exc}") from exc


def _agreement(
    a_path: Path, b_path: Path, size: tuple[int, int]
) -> tuple[float, float, list[dict[str, Any]], float, int]:
    a = _load_rgb(a_path, size)
    b = _load_rgb(b_path, size)

    difference = ImageChops.difference(a, b)

    def difference_score(image: Image.Image) -> float:
        means = ImageStat.Stat(image).mean
        value = 1.0 - sum(means) / (len(means) * 255.0)
        return min(1.0, max(0.0, float(value)))

    global_score = difference_score(difference)
    width, height = size
    def score_grid(columns: int, rows: int, label: str) -> list[dict[str, Any]]:
        tile_count = columns * rows
        if not 1 <= tile_count <= MAX_AGREEMENT_TILES:
            raise CyclesPreviewError(
                f"{label} agreement grid needs {tile_count} tiles; "
                f"limit is {MAX_AGREEMENT_TILES}"
            )
        values: list[dict[str, Any]] = []
        for row in range(rows):
            top = row * height // rows
            bottom = (row + 1) * height // rows
            for column in range(columns):
                left = column * width // columns
                right = (column + 1) * width // columns
                box = (left, top, right, bottom)
                values.append(
                    {"rect": box, "score": difference_score(difference.crop(box))}
                )
        if not values:
            raise CyclesPreviewError(f"{label} agreement grid produced no tiles")
        return values

    columns, rows = _agreement_grid(size)
    tiles = score_grid(columns, rows, "regional")
    micro_columns, micro_rows = _microtile_grid(size)
    microtiles = score_grid(micro_columns, micro_rows, "microtile")
    return (
        global_score,
        min(tile["score"] for tile in tiles),
        tiles,
        min(tile["score"] for tile in microtiles),
        len(microtiles),
    )


def _agreement_grid(size: tuple[int, int]) -> tuple[int, int]:
    """Return a balanced, aspect-preserving grid for one image size."""
    width, height = size
    long_edge = max(width, height)
    long_tiles = min(
        AGREEMENT_MAX_LONG_EDGE_TILES,
        max(1, long_edge // AGREEMENT_MIN_TILE_EDGE),
    )

    def scaled_tiles(edge: int) -> int:
        return max(1, (long_tiles * edge + long_edge // 2) // long_edge)

    return scaled_tiles(width), scaled_tiles(height)


def _agreement_grid_manifest(size: tuple[int, int]) -> dict[str, int]:
    columns, rows = _agreement_grid(size)
    return {
        "columns": columns,
        "rows": rows,
        "max_long_edge_tiles": AGREEMENT_MAX_LONG_EDGE_TILES,
        "minimum_nominal_edge_pixels": AGREEMENT_MIN_TILE_EDGE,
    }


def _microtile_grid(size: tuple[int, int]) -> tuple[int, int]:
    """Balanced fixed-scale cells with no tiny remainder sliver."""
    width, height = size
    return max(1, width // MICROTILE_EDGE), max(1, height // MICROTILE_EDGE)


def _microtile_grid_manifest(size: tuple[int, int]) -> dict[str, int]:
    columns, rows = _microtile_grid(size)
    return {
        "columns": columns,
        "rows": rows,
        "nominal_edge_pixels": MICROTILE_EDGE,
    }


def _context_for_descriptor(
    descriptor: Any, *, allowed_phases: frozenset[str]
) -> tuple[dict[str, Any], str]:
    """Resolve a stable descriptor back to its live, pinned request context."""
    if not isinstance(descriptor, dict):
        raise CyclesPreviewError("benchmark descriptor must be an object")
    for context in _CONTEXTS.values():
        for phase, expected in context["descriptors"].items():
            if phase in allowed_phases and descriptor == expected:
                return context, phase
    raise CyclesPreviewError("benchmark descriptor is not bound to this backend session")


def _artifact_from_manifest(
    context: dict[str, Any], phase: str
) -> tuple[Path, dict[str, Any]]:
    manifest_path = context["unit_dir"] / f"{phase}-manifest.json"
    try:
        raw = manifest_path.read_bytes()
        manifest = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CyclesPreviewError(f"cannot read benchmark {phase} manifest") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != ARTIFACT_SCHEMA_VERSION
        or manifest.get("kind") != MANIFEST_KIND
        or manifest.get("phase") != phase
        or manifest.get("binding_sha256") != context["binding"]
        or not isinstance(manifest.get("artifact"), dict)
        or not isinstance(manifest.get("render"), dict)
    ):
        raise CyclesPreviewError(f"benchmark {phase} manifest identity mismatch")
    artifact_row = manifest["artifact"]
    if set(artifact_row) != {"path", "sha256", "media_type"}:
        raise CyclesPreviewError(f"benchmark {phase} artifact shape mismatch")
    relative_raw = artifact_row["path"]
    if not isinstance(relative_raw, str) or not relative_raw:
        raise CyclesPreviewError(f"benchmark {phase} artifact path mismatch")
    relative = PurePosixPath(relative_raw)
    if (
        relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
        or artifact_row["media_type"] != "image/png"
    ):
        raise CyclesPreviewError(f"benchmark {phase} artifact path is unsafe")
    try:
        artifact = (context["session"]["output_root"] / Path(*relative.parts)).resolve(
            strict=True
        )
        artifact.relative_to(context["session"]["root"])
    except (OSError, ValueError) as exc:
        raise CyclesPreviewError(f"benchmark {phase} artifact escaped its session") from exc
    actual_sha = _validate_png(
        artifact,
        width=context["payload"]["width"],
        height=context["payload"]["height"],
    )
    if artifact_row["sha256"] != actual_sha:
        raise CyclesPreviewError(f"benchmark {phase} artifact digest mismatch")
    return artifact, manifest


def benchmark_equal(candidate: Any, baseline_descriptor: Any) -> bool:
    """Audit a delivered preview against a measured high-SPP denominator.

    Product acceptance is already complete before this benchmark-only hook runs.
    The reference can remove a speed claim, but it never selects the artifact.
    """
    context, candidate_phase = _context_for_descriptor(
        candidate, allowed_phases=frozenset({"draft", "repair"})
    )
    baseline_context, baseline_phase = _context_for_descriptor(
        baseline_descriptor, allowed_phases=frozenset({"baseline"})
    )
    if context is not baseline_context or baseline_phase != "baseline":
        raise CyclesPreviewError("benchmark candidate/baseline binding mismatch")
    candidate_path, candidate_manifest = _artifact_from_manifest(context, candidate_phase)
    baseline_path, baseline_manifest = _artifact_from_manifest(context, "baseline")
    size = (context["payload"]["width"], context["payload"]["height"])
    global_score, worst_score, tiles, micro_worst, micro_count = _agreement(
        candidate_path, baseline_path, size
    )
    global_wire = round(global_score, 9)
    worst_wire = round(worst_score, 9)
    micro_wire = round(micro_worst, 9)
    passed = (
        global_wire >= GLOBAL_AGREEMENT_MIN
        and worst_wire >= WORST_TILE_AGREEMENT_MIN
        and micro_wire >= MICROTILE_AGREEMENT_MIN
    )
    audit = {
        "schema_version": BENCHMARK_AUDIT_SCHEMA_VERSION,
        "kind": "cx_cycles_preview_benchmark_audit",
        "measurement_only": True,
        "product_decision_used_reference": False,
        "binding_sha256": context["binding"],
        "metric": "one_minus_mean_absolute_rgb_difference",
        "global_agreement": global_wire,
        "worst_tile_agreement": worst_wire,
        "global_min": GLOBAL_AGREEMENT_MIN,
        "worst_tile_min": WORST_TILE_AGREEMENT_MIN,
        "tile_grid": _agreement_grid_manifest(size),
        "tile_contract": "resolution_relative_regions_not_fixed_pixel_defects",
        "tile_count": len(tiles),
        "microtile_grid": _microtile_grid_manifest(size),
        "microtile_count": micro_count,
        "worst_microtile_agreement": micro_wire,
        "worst_microtile_min": MICROTILE_AGREEMENT_MIN,
        "microtile_contract": "fixed_scale_catastrophic_defect_sentinel",
        "passed": passed,
        "sample_ranges": {
            "draft": [0, context["payload"]["draft_samples"]],
            "verify": [
                context["payload"]["draft_samples"],
                context["payload"]["draft_samples"]
                + context["payload"]["verify_samples"],
            ],
            "baseline": [
                context["payload"]["draft_samples"]
                + context["payload"]["verify_samples"],
                context["payload"]["draft_samples"]
                + context["payload"]["verify_samples"]
                + context["payload"]["repair_samples"],
            ],
        },
        "sample_ranges_disjoint": candidate_phase == "draft",
        "candidate": {
            "phase": candidate_phase,
            "manifest_path": candidate["manifest_path"],
            "artifact_sha256": candidate_manifest["artifact"]["sha256"],
            "sample_offset": candidate_manifest["render"]["sample_offset"],
        },
        "baseline": {
            "phase": "baseline",
            "manifest_path": baseline_descriptor["manifest_path"],
            "artifact_sha256": baseline_manifest["artifact"]["sha256"],
            "sample_offset": baseline_manifest["render"]["sample_offset"],
        },
    }
    _write_manifest(context["unit_dir"] / "benchmark-audit.json", audit)
    context["benchmark_audit"] = audit
    return passed


def _rectangles_touch(
    left: tuple[int, int, int, int], right: tuple[int, int, int, int]
) -> bool:
    return (
        left[0] <= right[2]
        and right[0] <= left[2]
        and left[1] <= right[3]
        and right[1] <= left[3]
    )


def _coalesce_rectangles(
    rectangles: list[tuple[int, int, int, int]],
) -> list[tuple[int, int, int, int]]:
    merged: list[tuple[int, int, int, int]] = []
    for rectangle in sorted(rectangles):
        candidate = rectangle
        changed = True
        while changed:
            changed = False
            kept: list[tuple[int, int, int, int]] = []
            for existing in merged:
                if _rectangles_touch(candidate, existing):
                    candidate = (
                        min(candidate[0], existing[0]),
                        min(candidate[1], existing[1]),
                        max(candidate[2], existing[2]),
                        max(candidate[3], existing[3]),
                    )
                    changed = True
                else:
                    kept.append(existing)
            merged = kept
        merged.append(candidate)
    return sorted(merged)


def _select_repair_plan(
    global_score: float,
    tiles: list[dict[str, Any]],
    *,
    width: int,
    height: int,
) -> dict[str, Any]:
    failing = [tile for tile in tiles if tile["score"] < WORST_TILE_AGREEMENT_MIN]

    def full(reason: str) -> dict[str, Any]:
        return {
            "mode": "full_frame",
            "reason": reason,
            "failing_tile_count": len(failing),
            "rectangles": [],
            "repair_area_pixels": width * height,
            "repair_area_fraction": 1.0,
        }

    if not failing:
        return full(
            "global_agreement_failed_without_a_localizable_tile"
            if global_score < GLOBAL_AGREEMENT_MIN
            else "no_failing_tiles"
        )
    if len(failing) > MAX_SELECTIVE_FAILING_TILES:
        return full("failing_tile_limit_exceeded")

    expanded: list[tuple[int, int, int, int]] = []
    for tile in failing:
        left, top, right, bottom = tile["rect"]
        expanded.append(
            (
                max(0, left - REPAIR_HALO_PIXELS),
                max(0, top - REPAIR_HALO_PIXELS),
                min(width, right + REPAIR_HALO_PIXELS),
                min(height, bottom + REPAIR_HALO_PIXELS),
            )
        )
    rectangles = _coalesce_rectangles(expanded)
    if len(rectangles) > MAX_SELECTIVE_COMPONENTS:
        return full("component_limit_exceeded")
    area = sum(
        (right - left) * (bottom - top)
        for left, top, right, bottom in rectangles
    )
    fraction = area / (width * height)
    if fraction > MAX_SELECTIVE_AREA_FRACTION:
        return full("repair_area_limit_exceeded")
    return {
        "mode": "selective",
        "reason": "bounded_local_disagreement",
        "failing_tile_count": len(failing),
        "rectangles": rectangles,
        "repair_area_pixels": area,
        "repair_area_fraction": fraction,
    }


def _compose_patch_outputs(
    context: dict[str, Any],
    patches: list[dict[str, Any]],
    output: Path,
) -> str:
    payload = context["payload"]
    _assert_private_path(output, context["session"])
    if output.exists() or output.is_symlink():
        raise CyclesPreviewError("refusing to overwrite selective repair composite")
    size = (payload["width"], payload["height"])
    composite = _load_rgb(context["unit_dir"] / "draft.png", size)
    for patch in patches:
        rectangle = patch["rectangle"]
        patch_image = _load_rgb(patch["path"], size)
        composite.paste(patch_image.crop(rectangle), rectangle[:2])
    encoded = io.BytesIO()
    composite.save(encoded, format="PNG", optimize=False, compress_level=6)
    data = encoded.getvalue()
    artifact_limit = min(
        MAX_ARTIFACT_BYTES,
        payload["width"] * payload["height"] * 8 + (1 << 20),
    )
    if not 1 <= len(data) <= artifact_limit:
        raise CyclesPreviewError("selective repair composite exceeded its PNG bound")
    _write_new_file(output, data)
    return _validate_png(output, width=payload["width"], height=payload["height"])


def _proposal_context(proposal: DraftProposal) -> dict[str, Any]:
    if not isinstance(proposal, DraftProposal):
        raise CyclesPreviewError("verify/repair requires a DraftProposal")
    if set(proposal.meta) != {"cx_cycles_context"}:
        raise CyclesPreviewError("Cycles proposal metadata shape mismatch")
    token = proposal.meta["cx_cycles_context"]
    if not isinstance(token, str):
        raise CyclesPreviewError("Cycles proposal context token is invalid")
    context = _CONTEXTS.get(token)
    if context is None or proposal.unit is not context["unit"]:
        raise CyclesPreviewError("Cycles proposal is not bound to this backend session")
    if proposal.draft != context["descriptors"]["draft"]:
        raise CyclesPreviewError("Cycles proposal descriptor was modified")
    return context


def draft(unit: SpecUnit) -> DraftProposal:
    context = _context_for_unit(unit)
    output = context["unit_dir"] / "draft.png"
    samples = context["payload"]["draft_samples"]
    seed = context["seeds"]["draft"]
    artifact_sha = _invoke_blender(context, "draft", samples, seed, output)
    manifest = _artifact_manifest(
        context, "draft", output, artifact_sha, samples=samples, seed=seed
    )
    _write_manifest(context["unit_dir"] / "draft-manifest.json", manifest)
    return DraftProposal(
        unit,
        context["descriptors"]["draft"],
        {"cx_cycles_context": context["token"]},
    )


def verify(proposal: DraftProposal) -> Verification:
    context = _proposal_context(proposal)
    if context["verified"]:
        raise CyclesPreviewError("Cycles proposal was already verified")
    context["verified"] = True
    output = context["unit_dir"] / "verify.png"
    samples = context["payload"]["verify_samples"]
    seed = context["seeds"]["verify"]
    artifact_sha = _invoke_blender(context, "verify", samples, seed, output)
    draft_path = context["unit_dir"] / "draft.png"
    size = (context["payload"]["width"], context["payload"]["height"])
    global_score, worst_score, tile_scores, micro_worst, micro_count = _agreement(
        draft_path, output, size
    )
    accepted = (
        global_score >= GLOBAL_AGREEMENT_MIN
        and worst_score >= WORST_TILE_AGREEMENT_MIN
        and micro_worst >= MICROTILE_AGREEMENT_MIN
    )
    failing_tiles = sorted(
        (
            tile
            for tile in tile_scores
            if tile["score"] < WORST_TILE_AGREEMENT_MIN
        ),
        key=lambda tile: (tile["score"], tile["rect"]),
    )
    repair_plan = None
    if not accepted:
        repair_plan = _select_repair_plan(
            global_score,
            tile_scores,
            width=size[0],
            height=size[1],
        )
        if context["session"]["candidate_profile"]["name"] != "native":
            failing_count = sum(
                tile["score"] < WORST_TILE_AGREEMENT_MIN
                for tile in tile_scores
            )
            repair_plan = {
                "mode": "full_frame",
                "reason": "non_native_profile_requires_full_reference_repair",
                "failing_tile_count": failing_count,
                "rectangles": [],
                "repair_area_pixels": size[0] * size[1],
                "repair_area_fraction": 1.0,
            }
    context["repair_plan"] = repair_plan
    truth = (
        context["descriptors"]["draft"]
        if accepted
        else context["descriptors"]["repair"]
    )
    verification_token = secrets.token_hex(32)
    verification_manifest = {
        "schema_version": VERIFICATION_SCHEMA_VERSION,
        "kind": "cx_cycles_preview_verification",
        "preview_only": True,
        "billing_eligible": False,
        "production_ready": False,
        "artifact_verified": False,
        "evidence": "synthetic",
        "unit_id": context["unit"].unit_id,
        "binding_sha256": context["binding"],
        "independent_seed": True,
        "draft_seed": context["seeds"]["draft"],
        "verify_seed": seed,
        "sample_ranges_disjoint": True,
        "draft_sample_offset": 0,
        "verify_sample_offset": context["payload"]["draft_samples"],
        "verify_artifact": {
            "path": output.relative_to(context["session"]["output_root"]).as_posix(),
            "sha256": artifact_sha,
        },
        "metric": "one_minus_mean_absolute_rgb_difference",
        "global_agreement": round(global_score, 9),
        "worst_tile_agreement": round(worst_score, 9),
        "tile_grid": _agreement_grid_manifest(
            (context["payload"]["width"], context["payload"]["height"])
        ),
        "tile_contract": "resolution_relative_regions_not_fixed_pixel_defects",
        "tile_count": len(tile_scores),
        "microtile_grid": _microtile_grid_manifest(size),
        "microtile_count": micro_count,
        "worst_microtile_agreement": round(micro_worst, 9),
        "worst_microtile_min": MICROTILE_AGREEMENT_MIN,
        "microtile_contract": "fixed_scale_catastrophic_defect_sentinel",
        "failing_tile_count": len(failing_tiles),
        "failing_tiles": [
            {
                "agreement": round(tile["score"], 9),
                "rect": list(tile["rect"]),
            }
            for tile in failing_tiles[:MAX_RECORDED_FAILING_TILES]
        ],
        "failing_tiles_truncated": len(failing_tiles) > MAX_RECORDED_FAILING_TILES,
        "global_min": GLOBAL_AGREEMENT_MIN,
        "worst_tile_min": WORST_TILE_AGREEMENT_MIN,
        "repair_plan": (
            None
            if repair_plan is None
            else {
                **repair_plan,
                "rectangles": [list(rect) for rect in repair_plan["rectangles"]],
            }
        ),
        "accepted": accepted,
        "selected_manifest_path": truth["manifest_path"],
    }
    _write_manifest(
        context["unit_dir"] / "verification-manifest.json", verification_manifest
    )
    quality = min(global_score, worst_score)
    reason = (
        "independent low-SPP renders agree"
        if accepted
        else "independent low-SPP renders diverge; high-SPP repair required"
    )
    result = Verification(
        accepted,
        truth,
        quality=quality,
        reason=reason,
        meta={
            "cx_cycles_context": context["token"],
            "cx_cycles_verification": verification_token,
        },
    )
    _VERIFICATIONS[verification_token] = (context, result)
    return result


def repair(proposal: DraftProposal, verification: Verification) -> RepairResult:
    context = _proposal_context(proposal)
    if not isinstance(verification, Verification):
        raise CyclesPreviewError("repair requires a Verification")
    if set(verification.meta) != {
        "cx_cycles_context",
        "cx_cycles_verification",
    }:
        raise CyclesPreviewError("Cycles verification metadata shape mismatch")
    token = verification.meta["cx_cycles_verification"]
    stored = _VERIFICATIONS.get(token) if isinstance(token, str) else None
    if (
        stored is None
        or stored[0] is not context
        or stored[1] is not verification
        or verification.accepted
        or verification.truth != context["descriptors"]["repair"]
    ):
        raise CyclesPreviewError("repair is not bound to a rejected verification")
    if context["repaired"]:
        raise CyclesPreviewError("Cycles proposal was already repaired")
    context["repaired"] = True
    plan = context.get("repair_plan")
    if not isinstance(plan, dict) or plan.get("mode") not in {
        "selective",
        "full_frame",
    }:
        raise CyclesPreviewError("rejected Cycles verification has no repair plan")
    if (
        context["session"]["candidate_profile"]["name"] != "native"
        and plan["mode"] != "full_frame"
    ):
        raise CyclesPreviewError(
            "non-native candidate profiles require full-frame reference repair"
        )
    samples = context["payload"]["repair_samples"]
    seed = context["seeds"]["repair"]
    session = context["session"]
    strategy: dict[str, Any] = {
        "preview_only": True,
        "artifact_verified": False,
        "evidence": "synthetic",
        "attempted": plan["mode"],
        "disposition": "full_frame",
        "reason": plan["reason"],
        "initial_failing_tile_count": plan["failing_tile_count"],
        "halo_pixels": REPAIR_HALO_PIXELS,
        "planned_repair_area_pixels": plan["repair_area_pixels"],
        "planned_repair_area_fraction": round(plan["repair_area_fraction"], 9),
        "effective_repair_area_pixels": plan["repair_area_pixels"],
        "effective_repair_area_fraction": round(plan["repair_area_fraction"], 9),
        "rectangles": [list(rectangle) for rectangle in plan["rectangles"]],
        "patches": [],
        "post_patch_agreement": None,
        "full_frame_escalated": plan["mode"] != "selective",
    }
    output: Path | None = None
    artifact_sha: str | None = None
    source = "high_spp_cycles_full_repair"

    if plan["mode"] == "selective":
        patches: list[dict[str, Any]] = []
        try:
            for index, rectangle in enumerate(plan["rectangles"]):
                patch_output = context["unit_dir"] / f"repair-patch-{index:02d}.png"
                patch_sha = _invoke_blender(
                    context,
                    "repair",
                    samples,
                    seed,
                    patch_output,
                    border=rectangle,
                    execution_label=f"repair-patch-{index:02d}",
                )
                patches.append(
                    {
                        "path": patch_output,
                        "rectangle": rectangle,
                        "sha256": patch_sha,
                    }
                )
                strategy["patches"].append(
                    {
                        "rect": list(rectangle),
                        "samples": samples,
                        "seed": seed,
                        "artifact": {
                            "path": patch_output.relative_to(
                                session["output_root"]
                            ).as_posix(),
                            "sha256": patch_sha,
                        },
                    }
                )
            candidate = context["unit_dir"] / "repair-selective.png"
            candidate_sha = _compose_patch_outputs(context, patches, candidate)
            verify_path = context["unit_dir"] / "verify.png"
            size = (context["payload"]["width"], context["payload"]["height"])
            post_global, post_worst, _, post_micro, _ = _agreement(
                candidate, verify_path, size
            )
            post_passed = (
                post_global >= GLOBAL_AGREEMENT_MIN
                and post_worst >= WORST_TILE_AGREEMENT_MIN
                and post_micro >= MICROTILE_AGREEMENT_MIN
            )
            strategy["post_patch_agreement"] = {
                "metric": "one_minus_mean_absolute_rgb_difference",
                "independent_full_frame": True,
                "global": round(post_global, 9),
                "worst_tile": round(post_worst, 9),
                "global_min": GLOBAL_AGREEMENT_MIN,
                "worst_tile_min": WORST_TILE_AGREEMENT_MIN,
                "worst_microtile": round(post_micro, 9),
                "worst_microtile_min": MICROTILE_AGREEMENT_MIN,
                "passed": post_passed,
            }
            if post_passed:
                output = candidate
                artifact_sha = candidate_sha
                strategy["disposition"] = "selective_composite"
                strategy["composite_mixed_samples"] = True
                strategy["full_frame_escalated"] = False
                source = "high_spp_cycles_selective_repair"
            else:
                strategy["reason"] = "post_patch_full_frame_agreement_failed"
                strategy["full_frame_escalated"] = True
        except Exception as exc:
            strategy["reason"] = "selective_execution_failed"
            strategy["selective_error"] = (
                f"{type(exc).__name__}: {exc}"
            )[:500]
            strategy["full_frame_escalated"] = True

    if output is None or artifact_sha is None:
        output = context["unit_dir"] / "repair.png"
        artifact_sha = _invoke_blender(
            context,
            "repair",
            samples,
            seed,
            output,
            execution_label="repair-full",
        )
        strategy["disposition"] = "full_frame"
        strategy["composite_mixed_samples"] = False
        strategy["effective_repair_area_pixels"] = (
            context["payload"]["width"] * context["payload"]["height"]
        )
        strategy["effective_repair_area_fraction"] = 1.0

    context["repair_strategy"] = strategy
    manifest = _artifact_manifest(
        context,
        "repair",
        output,
        artifact_sha,
        samples=samples,
        seed=seed,
        repair_strategy=strategy,
    )
    _write_manifest(context["unit_dir"] / "repair-manifest.json", manifest)
    return RepairResult(
        context["descriptors"]["repair"],
        {"source": source},
    )


def baseline(unit: SpecUnit) -> dict[str, Any]:
    """Authoritative lazy fallback; never an always-run speed denominator."""
    context = _context_for_unit(unit)
    output = context["unit_dir"] / "baseline.png"
    samples = context["payload"]["repair_samples"]
    seed = context["seeds"]["baseline"]
    artifact_sha = _invoke_blender(context, "baseline", samples, seed, output)
    manifest = _artifact_manifest(
        context, "baseline", output, artifact_sha, samples=samples, seed=seed
    )
    _write_manifest(context["unit_dir"] / "baseline-manifest.json", manifest)
    return context["descriptors"]["baseline"]


# The first-party driver loads a fresh backend module for one request, then
# exits. Closing the command pipe lets an idle Blender return normally; the
# bounded cleanup path kills it if it does not. This prevents a successful Rust
# preview command from leaving a renderer behind after the driver has exited.
atexit.register(_shutdown_worker)
