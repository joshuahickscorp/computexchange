#!/usr/bin/env python3
"""Measure the frozen spatial75 render frontier against a 4096-SPP reference.

This is an experimental, local-unattested Apple Metal benchmark.  The product
lane is fixed to two independent 810x1440 4-SPP Cycles renders, the reference-
free spatial75 agreement gate, and the frozen 1080x1920 bicubic/bilinear PNG
operator.  Only after that delivery is complete does the harness render a
same-scene, same-frame 1080x1920 4096-SPP reference and run quality contract v3.

The candidate lane is measured first as an odd repeated-trial campaign after a
disclosed full-lane warmup.  A backend-retained draft decode is converted and
prepared while the independent verify render runs; the retained verify decode
then enters an in-memory pair gate, which must pass before immutable prepared
bytes publish.
The headline uses median candidate wall time, while quality is bound in advance
to trial zero rather than timing- or reference-selected.  A small different-
frame render then forces the single reference command back through the broad
resident mutation path.  Quality-v3 and its independent verifier are
measurement-only: they can invalidate a speed claim, but cannot select the
delivered artifact.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import secrets
import stat
import statistics
import sys
import time
from typing import Any, Sequence


HERE = Path(__file__).resolve().parent
BACKEND_PATH = HERE / "cx_cycles_render_preview_backend.py"
DRIVER_PATH = HERE / "cx_agent_render_preview_driver.py"
CORE_PATH = HERE / "cx_speculative_core.py"
ADAPTER_PATH = HERE / "cx_render_spec_adapter.py"
SPATIAL_PATH = HERE / "cx_render_spatial75_v1.py"
QUALITY_PATH = HERE / "cx_render_quality_v3.py"
QUALITY_VERIFIER_PATH = HERE / "verify_cx_render_quality_v3.py"
LOCAL_BENCHMARK_PATH = HERE / "run_local_cycles_spec_benchmark.py"
FRONTIER_TEST_PATH = HERE / "test_run_spatial75_cycles_frontier.py"
SPATIAL_TEST_PATH = HERE / "test_cx_render_spatial75_v1.py"
QUALITY_TEST_PATH = HERE / "test_cx_render_quality_v3.py"
QUALITY_VERIFIER_TEST_PATH = HERE / "test_verify_cx_render_quality_v3.py"
DEFAULT_BLENDER = Path("/Applications/Blender.app/Contents/MacOS/Blender")

SCHEMA_VERSION = 1
KIND = "cx_spatial75_cycles_frontier"
TIMING_EVIDENCE_KIND = "cx_spatial75_cycles_frontier_timing_evidence"
RECEIPT_TRUST = "local_unattested"
LOW_SIZE = (810, 1440)
DELIVERY_SIZE = (1080, 1920)
DRAFT_SAMPLES = 4
VERIFY_SAMPLES = 4
REFERENCE_SAMPLES = 4096
DEFAULT_CANDIDATE_TRIALS = 7
QUALITY_TRIAL_INDEX = 0
RESIDENT_POLICY = "same_frame_minimal_v1"
CANDIDATE_PROFILE = "native"

CANDIDATE_PROFILE_ENV = "CX_SPEC_RENDER_CYCLES_CANDIDATE_PROFILE"
CANDIDATE_PROFILE_SCOPE_ENV = "CX_SPEC_RENDER_CYCLES_CANDIDATE_PROFILE_SCOPE"
CANDIDATE_PROFILE_AUTH_ENV = "CX_SPEC_RENDER_CYCLES_CANDIDATE_PROFILE_AUTH"
RESIDENT_POLICY_ENV = "CX_SPEC_RENDER_CYCLES_RESIDENT_POLICY"
CANDIDATE_PROFILE_BENCHMARK_SCOPE = "benchmark_screen_v1"
BENCHMARK_PROFILE_META_KEY = "cx_benchmark_profile_auth_v1"
BENCHMARK_UNIT_ID = "local-metal-benchmark"

CLAIM_SCOPE = (
    "single-frame resident steady-state experimental preview ratio; "
    "median of predeclared candidate-first two-seed spatial75 trials "
    "versus one disclosed same-scene, same-frame broad-mutation "
    "4096-SPP Cycles reference"
)
TIMING_SCOPE = (
    "candidate wall includes both render endpoints, manifests, exact "
    "retained draft snapshot conversion/prepare overlapped with "
    "verify rendering, retained verify conversion and exact "
    "reference-free gate tail, validation and PNG0 publication; no "
    "overlapped work is subtracted; quality-v3 is measurement-only"
)
EXECUTION_ORDER = [
    "uncharged_full_candidate_warmup",
    "seven_or_operator_pinned_odd_measured_candidate_trials",
    "measurement_only_different_frame_broad_rearm",
    "measured_4096spp_reference",
    "measurement_only_quality_v3",
    "measurement_only_independent_quality_v3_verification",
]
NATIVE_CANDIDATE_PROFILE = {
    "adaptive_min_samples": None,
    "adaptive_threshold": None,
    "diffuse_bounces": None,
    "glossy_bounces": None,
    "max_bounces": None,
    "name": CANDIDATE_PROFILE,
    "transmission_bounces": None,
    "use_adaptive_sampling": False,
    "use_light_tree": None,
}
DENOISING_OFF_POLICY = {
    "denoiser": None,
    "denoising_input_passes": None,
    "denoising_prefilter": None,
    "denoising_quality": None,
    "denoising_use_gpu": None,
    "use_denoising": False,
    "view_layer_use_denoising": False,
}


class FrontierError(ValueError):
    """A stable benchmark construction or validation failure."""


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise FrontierError("receipt is not finite canonical JSON") from exc


def _positive_int(raw: str) -> int:
    value = int(raw, 10)
    if value <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", required=True, type=Path)
    parser.add_argument("--blender", type=Path, default=DEFAULT_BLENDER)
    parser.add_argument("--device", choices=("METAL", "CPU"), default="METAL")
    parser.add_argument("--frame", type=int, required=True)
    parser.add_argument("--timeout-secs", type=_positive_int, default=600)
    parser.add_argument(
        "--candidate-trials", type=_positive_int, default=DEFAULT_CANDIDATE_TRIALS
    )
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument(
        "--allow-untrusted-renderer",
        action="store_true",
        help="allow a fixture renderer; evidence is then synthetic",
    )
    return parser.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    if not args.scene.is_absolute() or args.scene.suffix != ".blend":
        raise FrontierError("--scene must be an absolute lowercase .blend path")
    if not args.scene.is_file() or args.scene.is_symlink():
        raise FrontierError("--scene must be a non-symlink regular file")
    if not args.blender.is_absolute() or not args.blender.is_file():
        raise FrontierError("--blender must be an absolute executable file")
    if not os.access(args.blender, os.X_OK):
        raise FrontierError("--blender is not executable")
    if not 0 <= args.frame <= 1_000_000:
        raise FrontierError("--frame must be in [0,1000000]")
    if not 1 <= args.timeout_secs <= 600:
        raise FrontierError("--timeout-secs must be in [1,600]")
    if not 3 <= args.candidate_trials <= 15 or args.candidate_trials % 2 == 0:
        raise FrontierError("--candidate-trials must be odd and in [3,15]")
    if not args.output_root.is_absolute():
        raise FrontierError("--output-root must be absolute")
    if args.output_root.exists() or args.output_root.is_symlink():
        raise FrontierError("--output-root must not already exist")
    if (
        not args.output_root.parent.is_dir()
        or args.output_root.parent.is_symlink()
    ):
        raise FrontierError("--output-root parent must be an existing non-symlink directory")
    if args.json_out is not None:
        if not args.json_out.is_absolute():
            raise FrontierError("--json-out must be absolute")
        if args.json_out.exists() or args.json_out.is_symlink():
            raise FrontierError("--json-out must not already exist")
        if args.json_out == args.output_root:
            raise FrontierError("--json-out cannot alias --output-root")
        if (
            args.json_out.parent == args.output_root
            and args.json_out.name != "receipt.json"
        ):
            raise FrontierError(
                "--json-out inside the new output root must be receipt.json"
            )
        if args.json_out.parent != args.output_root and (
            not args.json_out.parent.is_dir()
            or args.json_out.parent.is_symlink()
        ):
            raise FrontierError(
                "--json-out parent must exist or be the new output root"
            )


def _restore_environment(snapshot: dict[str, str | None]) -> None:
    for key, value in snapshot.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _relative_record(path: Path, root: Path) -> dict[str, Any]:
    try:
        root_resolved = root.resolve(strict=True)
        lexical_relative = path.relative_to(root)
    except (OSError, ValueError) as exc:
        raise FrontierError("artifact escaped output root") from exc
    if root.is_symlink() or not root_resolved.is_dir():
        raise FrontierError("artifact root is unsafe")
    cursor = root
    for part in lexical_relative.parts:
        cursor = cursor / part
        try:
            if cursor.is_symlink():
                raise FrontierError("artifact is not a non-symlink path")
        except OSError as exc:
            raise FrontierError("artifact is unreadable") from exc
    try:
        unresolved_info = path.lstat()
    except OSError as exc:
        raise FrontierError("artifact is unreadable") from exc
    if not os.path.isfile(path) or path.is_symlink():
        raise FrontierError("artifact is not a non-symlink regular file")
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise FrontierError("artifact escaped output root") from exc
    wire = PurePosixPath(relative.as_posix())
    if any(part in {"", ".", ".."} for part in wire.parts):
        raise FrontierError("artifact relative path is unsafe")
    info = resolved.stat()
    if (
        unresolved_info.st_dev != info.st_dev
        or unresolved_info.st_ino != info.st_ino
        or unresolved_info.st_size != info.st_size
        or info.st_size <= 0
    ):
        raise FrontierError("artifact is not a nonempty regular file")
    digest = sha256_file(resolved)
    after = path.lstat()
    if (
        after.st_dev != unresolved_info.st_dev
        or after.st_ino != unresolved_info.st_ino
        or after.st_size != unresolved_info.st_size
        or after.st_mtime_ns != unresolved_info.st_mtime_ns
        or after.st_ctime_ns != unresolved_info.st_ctime_ns
    ):
        raise FrontierError("artifact changed while its receipt was computed")
    return {
        "path": wire.as_posix(),
        "bytes": info.st_size,
        "sha256": digest,
    }


def _write_new_json(path: Path, value: dict[str, Any]) -> None:
    """Durably create one JSON file without replacement.

    The destination descriptor remains open through the directory fsync and is
    checked against the exposed pathname. Same-UID writers remain outside the
    storage threat model, but an observed substitution fails closed and the
    writer only rolls back the inode it created.
    """

    encoded = json.dumps(
        value,
        sort_keys=True,
        indent=2,
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii") + b"\n"
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise FrontierError("JSON output parent is not a safe existing directory")
    if path.exists() or path.is_symlink():
        raise FrontierError("refusing to replace existing JSON output")
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    opened = os.fstat(descriptor)
    created_identity: tuple[int, int] | None = (opened.st_dev, opened.st_ino)
    publication_complete = False
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        written = os.fstat(descriptor)
        if (written.st_dev, written.st_ino) != created_identity:
            raise FrontierError("JSON output publication identity mismatch")
        exposed = path.lstat()
        if (
            not stat.S_ISREG(written.st_mode)
            or written.st_size != len(encoded)
            or exposed.st_dev != written.st_dev
            or exposed.st_ino != written.st_ino
            or exposed.st_size != len(encoded)
            or not stat.S_ISREG(exposed.st_mode)
        ):
            raise FrontierError("JSON output publication identity mismatch")

        directory_descriptor = os.open(
            path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        after_fsync = os.fstat(descriptor)
        exposed_after_fsync = path.lstat()
        if (
            (after_fsync.st_dev, after_fsync.st_ino) != created_identity
            or after_fsync.st_size != len(encoded)
            or exposed_after_fsync.st_dev != after_fsync.st_dev
            or exposed_after_fsync.st_ino != after_fsync.st_ino
            or exposed_after_fsync.st_size != len(encoded)
            or not stat.S_ISREG(exposed_after_fsync.st_mode)
        ):
            raise FrontierError("JSON output publication identity mismatch")
        publication_complete = True
    finally:
        os.close(descriptor)
        if not publication_complete and created_identity is not None:
            try:
                current = path.lstat()
                if (current.st_dev, current.st_ino) == created_identity:
                    path.unlink()
                    directory_descriptor = os.open(
                        path.parent,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                    )
                    try:
                        os.fsync(directory_descriptor)
                    finally:
                        os.close(directory_descriptor)
            except OSError:
                pass


def _reject_json_constant(value: str) -> None:
    raise FrontierError(f"non-finite JSON constant {value!r}")


def _reject_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FrontierError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if not 1 <= len(raw) <= 1024 * 1024:
            raise FrontierError("attestation JSON size is invalid")
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_object,
            parse_constant=_reject_json_constant,
        )
    except FrontierError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise FrontierError("attestation JSON is malformed") from exc
    if not isinstance(value, dict):
        raise FrontierError("attestation JSON must be an object")
    canonical_json(value)
    return value


def _validate_render_config(
    path: Path,
    *,
    phase: str,
    frame: int,
    size: tuple[int, int],
    samples: int,
    sample_offset: int,
    seed: int,
    output: Path,
) -> dict[str, Any]:
    config = _read_json_object(path)
    expected_keys = {
        "border",
        "candidate_denoising_policy",
        "candidate_profile",
        "frame",
        "height",
        "output",
        "phase",
        "resident_policy",
        "sample_offset",
        "samples",
        "seed",
        "width",
    }
    if set(config) != expected_keys:
        raise FrontierError("render config shape mismatch")
    candidate_profile = config.get("candidate_profile")
    denoising = config.get("candidate_denoising_policy")
    if (
        not isinstance(candidate_profile, dict)
        or not isinstance(denoising, dict)
        or config["border"] is not None
        or config["phase"] != phase
        or config["frame"] != frame
        or config["width"] != size[0]
        or config["height"] != size[1]
        or config["samples"] != samples
        or config["sample_offset"] != sample_offset
        or config["seed"] != seed
        or config["output"] != str(output)
        or config["resident_policy"] != RESIDENT_POLICY
        or candidate_profile != NATIVE_CANDIDATE_PROFILE
        or denoising != DENOISING_OFF_POLICY
    ):
        raise FrontierError("render config identity mismatch")
    return config


def _validate_render_manifest(
    path: Path,
    *,
    phase: str,
    frame: int,
    size: tuple[int, int],
    samples: int,
    sample_offset: int,
    seed: int,
    mutation: str,
    artifact: Path,
    artifact_sha256: str,
    output_root: Path,
) -> dict[str, Any]:
    manifest = _read_json_object(path)
    render = manifest.get("render")
    artifact_row = manifest.get("artifact")
    if not isinstance(render, dict) or not isinstance(artifact_row, dict):
        raise FrontierError("render manifest shape mismatch")
    expected_artifact = artifact.resolve(strict=True).relative_to(
        output_root.resolve(strict=True)
    ).as_posix()
    worker = render.get("worker_renderer_identity")
    integrator = render.get("integrator_policy")
    denoising = render.get("denoising_policy")
    if not isinstance(worker, dict):
        raise FrontierError("render worker identity shape mismatch")
    worker_profile = worker.get("candidate_profile")
    if not isinstance(worker_profile, dict):
        raise FrontierError("render worker profile shape mismatch")
    if (
        manifest.get("kind") != "cx_cycles_preview_manifest"
        or manifest.get("phase") != phase
        or render.get("frame") != frame
        or render.get("width") != size[0]
        or render.get("height") != size[1]
        or render.get("samples") != samples
        or render.get("sample_offset") != sample_offset
        or render.get("seed") != seed
        or render.get("resident_policy") != RESIDENT_POLICY
        or render.get("resident_mutation") != mutation
        or artifact_row.get("path") != expected_artifact
        or artifact_row.get("sha256") != artifact_sha256
        or worker.get("resident_policy") != RESIDENT_POLICY
        or worker_profile.get("name") != CANDIDATE_PROFILE
        or not isinstance(integrator, dict)
        or integrator.get("mode") != "fixed_reference"
        or integrator.get("actual_integrator") != worker.get("native_integrator")
        or integrator.get("actual_sampling") != worker.get("reference_sampling")
        or not isinstance(denoising, dict)
        or denoising.get("mode") != "fixed_off_reference"
        or denoising.get("actual") != worker.get("reference_denoising_policy")
    ):
        raise FrontierError("render manifest identity mismatch")
    return manifest


def _unit(core: Any, scene: Path, scene_sha: str, frame: int, size: tuple[int, int], auth: str, *, draft_samples: int = DRAFT_SAMPLES, verify_samples: int = VERIFY_SAMPLES, repair_samples: int = REFERENCE_SAMPLES) -> Any:
    return core.SpecUnit(
        BENCHMARK_UNIT_ID,
        "render",
        {
            "scene_path": scene.name,
            "scene_sha256": scene_sha,
            "width": size[0],
            "height": size[1],
            "frame": frame,
            "draft_samples": draft_samples,
            "verify_samples": verify_samples,
            "repair_samples": repair_samples,
        },
        {BENCHMARK_PROFILE_META_KEY: auth},
    )


def _render_with_manifest(
    backend: Any,
    context: dict[str, Any],
    phase: str,
    samples: int,
    seed: int,
    output_name: str,
    manifest_name: str,
    execution_label: str,
    *,
    retain_validated_png: bool = False,
) -> tuple[Path, Path, str, float, Any | None]:
    output = context["unit_dir"] / output_name
    manifest_path = context["unit_dir"] / manifest_name
    started = time.perf_counter()
    retained = None
    try:
        artifact_sha = backend._invoke_blender(
            context,
            phase,
            samples,
            seed,
            output,
            execution_label=execution_label,
            retain_validated_png=retain_validated_png,
        )
        manifest = backend._artifact_manifest(
            context,
            phase,
            output,
            artifact_sha,
            samples=samples,
            seed=seed,
        )
        backend._write_manifest(manifest_path, manifest)
        if retain_validated_png:
            retained = backend._pop_retained_validated_png(
                context,
                phase=phase,
                path=output,
                sha256=artifact_sha,
            )
        elif sha256_file(output) != artifact_sha:
            raise FrontierError(
                f"{phase} artifact changed after manifest publication"
            )
        elapsed = time.perf_counter() - started
        return output, manifest_path, artifact_sha, elapsed, retained
    except BaseException:
        if retain_validated_png:
            backend._discard_retained_validated_pngs(context)
        raise


def _snapshot_receipt(snapshot: Any) -> dict[str, Any]:
    return {
        "source_bytes": len(snapshot.source_bytes),
        "sha256": snapshot.sha256,
        "mode": snapshot.mode,
        "pixel_bytes": len(snapshot.pixel_bytes),
        "pixel_sha256": snapshot.pixel_sha256,
    }


def _assert_no_retained_pngs(
    backend: Any, contexts: Sequence[dict[str, Any]]
) -> None:
    key = backend._RETAINED_VALIDATED_PNGS_KEY
    if any(key in context for context in contexts):
        for context in contexts:
            backend._discard_retained_validated_pngs(context)
        raise FrontierError("retained validated PNG cache was not fully consumed")


def _validate_snapshot_receipt(
    value: Any,
    *,
    artifact_sha256: str,
    artifact_bytes: int,
    size: tuple[int, int] = LOW_SIZE,
) -> None:
    if not isinstance(value, dict) or set(value) != {
        "mode",
        "pixel_bytes",
        "pixel_sha256",
        "sha256",
        "source_bytes",
    }:
        raise FrontierError("retained snapshot receipt shape mismatch")
    mode = value.get("mode")
    channels = 3 if mode == "RGB" else (4 if mode == "RGBA" else 0)
    if (
        channels == 0
        or value.get("sha256") != artifact_sha256
        or value.get("source_bytes") != artifact_bytes
        or type(value.get("pixel_bytes")) is not int
        or value["pixel_bytes"] != size[0] * size[1] * channels
        or not isinstance(value.get("pixel_sha256"), str)
        or len(value["pixel_sha256"]) != 64
        or any(
            character not in "0123456789abcdef"
            for character in value["pixel_sha256"]
        )
    ):
        raise FrontierError("retained snapshot receipt identity mismatch")


def _decoded_from_backend_snapshot(spatial: Any, snapshot: Any) -> Any:
    return spatial.decoded_png_from_backend_snapshot(
        png_bytes=snapshot.source_bytes,
        png_sha256=snapshot.sha256,
        png_byte_count=len(snapshot.source_bytes),
        decoder_mode=snapshot.mode,
        decoder_pixel_bytes=snapshot.pixel_bytes,
        decoder_pixel_sha256=snapshot.pixel_sha256,
    )


def _prepare_draft(spatial: Any, draft_snapshot: Any) -> dict[str, Any]:
    started = time.perf_counter()
    decoded_started = time.perf_counter()
    decoded = _decoded_from_backend_snapshot(spatial, draft_snapshot)
    decoded_finished = time.perf_counter()
    prepared = spatial.prepare_decoded_draft(decoded)
    return {
        "decoded": decoded,
        "snapshot": _snapshot_receipt(draft_snapshot),
        "snapshot_conversion_s": decoded_finished - decoded_started,
        "prepared": prepared,
        "started": started,
        "finished": time.perf_counter(),
    }


def _pipelined_spatial_result(
    spatial: Any,
    prepared_bundle: dict[str, Any],
    decoded_verify: Any,
    delivery_path: Path,
) -> tuple[Any, dict[str, Any]]:
    fused_started = time.perf_counter()
    try:
        fused = spatial.gate_decoded_pair_and_publish_prepared(
            prepared_bundle["prepared"],
            prepared_bundle["decoded"],
            decoded_verify,
            delivery_path,
        )
    except spatial.GateRejected as exc:
        if delivery_path.exists() or delivery_path.is_symlink():
            raise FrontierError("rejected spatial gate published an output")
        raise FrontierError(
            "candidate draft/verify gate rejected before publication"
        ) from exc
    fused_finished = time.perf_counter()
    gate = fused.gate
    postprocess = fused.postprocess
    if not gate.passed:
        raise FrontierError("fused spatial gate returned an unauthorized result")
    timings = {
        "read": 0,
        "decode": 0,
        "gate_difference": gate.timings_ns["difference"],
        "gate_partition": gate.timings_ns["partition"],
        "gate_score": gate.timings_ns["score"],
        "transform": postprocess.timings_ns["transform"],
        "encode": postprocess.timings_ns["encode"],
        "validate": postprocess.timings_ns["validate"],
        "publish": postprocess.timings_ns["publish"],
        "gate": gate.timings_ns["total"],
        "selected_draft_postprocess": postprocess.timings_ns["total"],
        "total": (
            prepared_bundle["prepared"].timings_ns.total
            + fused.timings_ns["total"]
        ),
    }
    result = spatial.BenchmarkResult(
        gate=gate, postprocess=postprocess, timings_ns=timings
    )
    return result, {
        "draft_snapshot_convert_and_prepare_s": (
            prepared_bundle["finished"] - prepared_bundle["started"]
        ),
        "draft_snapshot_conversion_s": prepared_bundle[
            "snapshot_conversion_s"
        ],
        "draft_snapshot": prepared_bundle["snapshot"],
        "gate_tail_s": gate.timings_ns["total"] / 1_000_000_000,
        "publish_tail_s": fused.timings_ns["publish"] / 1_000_000_000,
        "fused_authorization_tail_s": (
            fused.timings_ns["authorization"] / 1_000_000_000
        ),
        "fused_publish_tail_s": (
            fused.timings_ns["publish"] / 1_000_000_000
        ),
        "fused_gate_publish_tail_s": fused_finished - fused_started,
        "prepared": prepared_bundle["prepared"].receipt(),
    }


def _validate_execution_trace(
    backend: Any,
    low_context: dict[str, Any],
    rearm_context: dict[str, Any],
    full_context: dict[str, Any],
    *,
    frame: int,
    rearm_frame: int,
    capability: str,
    candidate_trials: int,
) -> dict[str, Any]:
    expected: list[tuple[int, int, str, str]] = [
        (1, frame, "draft", backend.RESIDENT_POLICY_BROAD),
        (2, frame, "verify", RESIDENT_POLICY),
    ]
    for trial in range(candidate_trials):
        expected.extend(
            (
                (3 + trial * 2, frame, "draft", RESIDENT_POLICY),
                (4 + trial * 2, frame, "verify", RESIDENT_POLICY),
            )
        )
    rearm_command_id = 3 + candidate_trials * 2
    baseline_command_id = rearm_command_id + 1
    expected.extend(
        (
            (
                rearm_command_id,
                rearm_frame,
                "draft",
                backend.RESIDENT_POLICY_BROAD,
            ),
            (
                baseline_command_id,
                frame,
                "baseline",
                backend.RESIDENT_POLICY_BROAD,
            ),
        )
    )
    observed = [
        *low_context.get("resident_mutation_history", ()),
        *rearm_context.get("resident_mutation_history", ()),
        *full_context.get("resident_mutation_history", ()),
    ]
    wire = [
        (
            row.get("command_id"),
            row.get("frame"),
            row.get("phase"),
            row.get("mutation"),
        )
        for row in observed
        if isinstance(row, dict)
    ]
    if len(observed) != len(wire) or wire != expected:
        raise FrontierError("resident worker command sequence mismatch")
    worker = getattr(backend, "_WORKER", None)
    if (
        not isinstance(worker, dict)
        or worker.get("commands") != baseline_command_id
        or worker.get("process") is None
        or worker["process"].poll() is not None
    ):
        raise FrontierError("resident worker was not reused for the full command trace")
    expected_key = backend._worker_key(low_context)
    if (
        worker.get("key") != expected_key
        or backend._worker_key(rearm_context) != expected_key
        or backend._worker_key(full_context) != expected_key
    ):
        raise FrontierError("resident worker key changed across benchmark contexts")
    contexts = (low_context, rearm_context, full_context)
    if any(context.get("session") is not low_context.get("session") for context in contexts):
        raise FrontierError("benchmark contexts did not share one backend session")
    if any(
        context["session"].get("candidate_profile_auth") != capability
        or context["session"].get("candidate_profile_scope")
        != CANDIDATE_PROFILE_BENCHMARK_SCOPE
        or context["session"].get("resident_policy") != RESIDENT_POLICY
        or context["session"].get("candidate_profile", {}).get("name")
        != CANDIDATE_PROFILE
        for context in contexts
    ):
        raise FrontierError("private benchmark capability/session binding mismatch")
    if any(
        context.get("scene_copy") != low_context.get("scene_copy")
        or context.get("scene_bundle", {}).get("sha256")
        != low_context.get("scene_bundle", {}).get("sha256")
        for context in contexts
    ):
        raise FrontierError("benchmark contexts did not share one pinned scene bundle")
    seeds = {
        low_context["seeds"]["draft"],
        low_context["seeds"]["verify"],
        full_context["seeds"]["baseline"],
    }
    if len(seeds) != 3:
        raise FrontierError("candidate/reference render seeds are not distinct")
    return {
        "worker_reused": True,
        "worker_command_count": baseline_command_id,
        "worker_key_sha256": hashlib.sha256(
            canonical_json(list(expected_key))
        ).hexdigest(),
        "same_backend_session": True,
        "same_scene_bundle": True,
        "different_frame_rearm": rearm_frame,
        "commands": [
            {
                "command_id": command_id,
                "frame": command_frame,
                "phase": phase,
                "mutation": mutation,
            }
            for command_id, command_frame, phase, mutation in expected
        ],
        "candidate_reference_seeds_distinct": True,
        "candidate_reference_sample_ranges_disjoint": True,
        "retained_validated_png_cache_empty": True,
        "candidate_handoff": "one_shot_backend_validated_png_snapshots",
    }


def _code_pins() -> dict[str, str]:
    return {
        "frontier_harness_sha256": sha256_file(Path(__file__).resolve()),
        "backend_sha256": sha256_file(BACKEND_PATH),
        "render_preview_driver_sha256": sha256_file(DRIVER_PATH),
        "controller_core_sha256": sha256_file(CORE_PATH),
        "controller_adapter_sha256": sha256_file(ADAPTER_PATH),
        "spatial75_module_sha256": sha256_file(SPATIAL_PATH),
        "quality_v3_module_sha256": sha256_file(QUALITY_PATH),
        "quality_v3_verifier_sha256": sha256_file(QUALITY_VERIFIER_PATH),
        "local_benchmark_identity_helper_sha256": sha256_file(LOCAL_BENCHMARK_PATH),
        "frontier_harness_test_sha256": sha256_file(FRONTIER_TEST_PATH),
        "spatial75_test_sha256": sha256_file(SPATIAL_TEST_PATH),
        "quality_v3_test_sha256": sha256_file(QUALITY_TEST_PATH),
        "quality_v3_verifier_test_sha256": sha256_file(
            QUALITY_VERIFIER_TEST_PATH
        ),
    }


def _finite_positive(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0.0
    ):
        raise FrontierError(f"{label} must be finite and positive")
    return float(value)


def _type7_quantile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(_finite_positive(value, "trial timing") for value in values)
    if not ordered or not 0.0 <= quantile <= 1.0:
        raise FrontierError("invalid timing quantile input")
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _trial_timing_projection(trial: dict[str, Any]) -> dict[str, Any]:
    component = trial.get("component_s")
    pipeline = trial.get("pipeline_overlap")
    spatial = trial.get("spatial75")
    if (
        not isinstance(component, dict)
        or set(component)
        != {
            "draft_endpoint_and_manifest",
            "post_verify_wait_gate_and_publish",
            "verify_endpoint_and_manifest",
        }
        or not isinstance(pipeline, dict)
        or not isinstance(spatial, dict)
    ):
        raise FrontierError("candidate trial timing evidence is incomplete")
    component_values = {
        key: _finite_positive(value, f"{key} timing")
        for key, value in component.items()
    }
    spec_s = _finite_positive(trial.get("spec_s"), "trial spec_s")
    component_sum = math.fsum(component_values.values())
    unattributed = spec_s - component_sum
    if not math.isfinite(unattributed) or unattributed < 0.0:
        raise FrontierError("trial wall time omits a measured component")
    pipeline_timings = {
        key: value
        for key, value in sorted(pipeline.items())
        if key.endswith("_s")
    }
    if not pipeline_timings or any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0.0
        for value in pipeline_timings.values()
    ):
        raise FrontierError("pipeline timing projection is invalid")
    try:
        spatial_timings = spatial["timings_ns"]
        gate_timings = spatial["gate"]["timings_ns"]
        postprocess_timings = spatial["postprocess"]["timings_ns"]
        prepared_timings = pipeline["prepared"]["timings_ns"]
    except (KeyError, TypeError) as exc:
        raise FrontierError("spatial timing projection is incomplete") from exc
    for label, timings in (
        ("spatial", spatial_timings),
        ("gate", gate_timings),
        ("postprocess", postprocess_timings),
        ("prepared", prepared_timings),
    ):
        if not isinstance(timings, dict) or any(
            type(value) is not int or value < 0 for value in timings.values()
        ):
            raise FrontierError(f"{label} nanosecond timing projection is invalid")

    def equal_seconds(observed_key: str, nanoseconds: int) -> bool:
        return math.isclose(
            float(pipeline_timings.get(observed_key, -1.0)),
            nanoseconds / 1_000_000_000,
            rel_tol=1e-12,
            abs_tol=1e-12,
        )

    post_verify_wall = component_values["post_verify_wait_gate_and_publish"]
    if (
        not math.isclose(
            post_verify_wall,
            float(
                pipeline_timings.get(
                    "post_verify_snapshot_gate_publish_wall_s", -1.0
                )
            ),
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
        or not equal_seconds("gate_tail_s", gate_timings.get("total", -1))
        or not equal_seconds(
            "publish_tail_s", postprocess_timings.get("publish", -1)
        )
        or not equal_seconds(
            "fused_publish_tail_s", postprocess_timings.get("publish", -1)
        )
        or not equal_seconds(
            "fused_authorization_tail_s", gate_timings.get("authorization", -1)
        )
        or spatial_timings.get("gate") != gate_timings.get("total")
        or spatial_timings.get("publish") != postprocess_timings.get("publish")
        or spatial_timings.get("selected_draft_postprocess")
        != postprocess_timings.get("total")
        or postprocess_timings.get("prepared_total")
        != prepared_timings.get("total")
    ):
        raise FrontierError("spatial/component timing binding mismatch")
    minimum_post_verify_wall = math.fsum(
        (
            float(pipeline_timings.get("verify_snapshot_conversion_s", -1.0)),
            float(pipeline_timings.get("prepare_wait_tail_s", -1.0)),
            float(pipeline_timings.get("fused_gate_publish_tail_s", -1.0)),
        )
    )
    prepared_total_s = prepared_timings.get("total", -1) / 1_000_000_000
    draft_prepare_minimum = math.fsum(
        (
            float(pipeline_timings.get("draft_snapshot_conversion_s", -1.0)),
            prepared_total_s,
        )
    )
    if (
        post_verify_wall < minimum_post_verify_wall
        or float(pipeline_timings.get("fused_gate_publish_tail_s", -1.0))
        < math.fsum(
            (
                float(pipeline_timings.get("gate_tail_s", -1.0)),
                float(pipeline_timings.get("publish_tail_s", -1.0)),
            )
        )
        or float(pipeline_timings.get("verify_endpoint_wall_s", -1.0))
        < component_values["verify_endpoint_and_manifest"]
        or float(
            pipeline_timings.get("draft_snapshot_convert_and_prepare_s", -1.0)
        )
        < draft_prepare_minimum
        or float(pipeline_timings.get("prepare_submit_to_ready_s", -1.0))
        < float(
            pipeline_timings.get("draft_snapshot_convert_and_prepare_s", -1.0)
        )
        or float(pipeline_timings.get("prepare_verify_overlap_s", -1.0))
        > float(pipeline_timings.get("prepare_submit_to_ready_s", -1.0))
        or float(pipeline_timings.get("prepare_verify_overlap_s", -1.0))
        > float(pipeline_timings.get("verify_endpoint_wall_s", -1.0))
    ):
        raise FrontierError("pipeline wall/component arithmetic is incomplete")
    return {
        "trial_index": trial.get("trial_index"),
        "spec_s": spec_s,
        "component_s": component_values,
        "component_sum_s": component_sum,
        "unattributed_wall_s": unattributed,
        "pipeline_timing_projection_s": pipeline_timings,
        "spatial_timings_ns": spatial_timings,
        "gate_timings_ns": gate_timings,
        "postprocess_timings_ns": postprocess_timings,
        "prepared_timings_ns": prepared_timings,
    }


def _timing_evidence(
    *, baseline_s: float, trials: Sequence[dict[str, Any]], pins: dict[str, str]
) -> dict[str, Any]:
    baseline = _finite_positive(baseline_s, "baseline timing evidence")
    projections = [_trial_timing_projection(trial) for trial in trials]
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": TIMING_EVIDENCE_KIND,
        "frontier_harness_sha256": pins["frontier_harness_sha256"],
        "baseline_s": baseline,
        "trial_count": len(projections),
        "candidate_spec_s_samples": [row["spec_s"] for row in projections],
        "trials": projections,
    }


def _artifact_path(record: dict[str, Any], output_root: Path) -> Path:
    _validate_artifact_record(record, output_root)
    relative = PurePosixPath(record["path"])
    return output_root / Path(*relative.parts)


def _recompute_quality_verification(
    proof_path: Path, candidate_path: Path, reference_path: Path
) -> dict[str, Any]:
    try:
        import verify_cx_render_quality_v3 as verifier  # noqa: PLC0415

        result = verifier.verify_paths(proof_path, candidate_path, reference_path)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:
        raise FrontierError("independent quality-v3 replay failed") from exc
    if not isinstance(result, dict):
        raise FrontierError("independent quality-v3 replay returned invalid data")
    return result


def _validate_artifact_record(
    record: Any, output_root: Path | None
) -> None:
    if not isinstance(record, dict) or set(record) != {"path", "bytes", "sha256"}:
        raise FrontierError("artifact record shape mismatch")
    relative = PurePosixPath(record["path"]) if isinstance(record["path"], str) else None
    if (
        relative is None
        or relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
        or type(record["bytes"]) is not int
        or record["bytes"] <= 0
        or not isinstance(record["sha256"], str)
        or len(record["sha256"]) != 64
        or any(character not in "0123456789abcdef" for character in record["sha256"])
    ):
        raise FrontierError("artifact record identity mismatch")
    if output_root is not None:
        actual = _relative_record(
            output_root / Path(*relative.parts), output_root
        )
        if actual != record:
            raise FrontierError("artifact record does not match published bytes")


def _is_lower_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _gate_semantics(gate: Any) -> dict[str, Any]:
    if not isinstance(gate, dict):
        raise FrontierError("spatial75 gate receipt is not an object")
    keys = {
        "binding_sha256",
        "draft",
        "global_agreement",
        "metric",
        "microtile_count",
        "passed",
        "policy_id",
        "policy_sha256",
        "runtime",
        "runtime_sha256",
        "thresholds",
        "tiles",
        "verify",
        "worst_microtile_agreement",
        "worst_tile_agreement",
    }
    if not keys.issubset(gate):
        raise FrontierError("spatial75 gate semantic fields are incomplete")
    return {key: gate[key] for key in sorted(keys)}


def _validate_spatial_artifact_binding(
    spatial_receipt: Any,
    artifacts: dict[str, Any],
    *,
    output_root: Path,
    pins: dict[str, str],
) -> None:
    try:
        import cx_render_spatial75_v1 as spatial  # noqa: PLC0415
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            raise
        raise FrontierError("spatial75 implementation could not be loaded") from exc

    if not isinstance(spatial_receipt, dict) or set(spatial_receipt) != {
        "gate",
        "kind",
        "policy",
        "policy_sha256",
        "postprocess",
        "runtime",
        "schema_version",
        "timings_ns",
    }:
        raise FrontierError("spatial75 receipt shape mismatch")
    expected_policy = spatial.POLICY_DESCRIPTOR
    expected_policy_sha = hashlib.sha256(canonical_json(expected_policy)).hexdigest()
    expected_runtime = spatial.runtime_identity()
    expected_runtime_sha = hashlib.sha256(canonical_json(expected_runtime)).hexdigest()
    if (
        spatial_receipt.get("schema_version") != spatial.SCHEMA_VERSION
        or spatial_receipt.get("kind") != spatial.RESULT_KIND
        or spatial_receipt.get("policy") != expected_policy
        or spatial_receipt.get("policy_sha256") != expected_policy_sha
        or spatial_receipt.get("runtime") != expected_runtime
        or expected_runtime.get("module_sha256")
        != pins["spatial75_module_sha256"]
    ):
        raise FrontierError("spatial75 policy/runtime identity mismatch")

    gate = spatial_receipt["gate"]
    postprocess = spatial_receipt["postprocess"]
    if not isinstance(gate, dict) or set(gate) != {
        "binding_sha256",
        "draft",
        "global_agreement",
        "metric",
        "microtile_count",
        "passed",
        "policy_id",
        "policy_sha256",
        "runtime",
        "runtime_sha256",
        "thresholds",
        "tiles",
        "timings_ns",
        "verify",
        "worst_microtile_agreement",
        "worst_tile_agreement",
    }:
        raise FrontierError("spatial75 gate shape mismatch")
    if not isinstance(postprocess, dict) or set(postprocess) != {
        "encoding",
        "experimental",
        "input",
        "input_decode_reused",
        "operators",
        "output",
        "policy_id",
        "policy_sha256",
        "quality_claim",
        "runtime",
        "timings_ns",
    }:
        raise FrontierError("spatial75 postprocess shape mismatch")

    draft_record = artifacts["draft"]
    verify_record = artifacts["verify"]
    delivery_record = artifacts["delivery"]
    draft_path = _artifact_path(draft_record, output_root)
    verify_path = _artifact_path(verify_record, output_root)
    delivery_path = _artifact_path(delivery_record, output_root)
    output = postprocess.get("output")
    if (
        gate.get("passed") is not True
        or gate.get("policy_id") != spatial.POLICY_ID
        or gate.get("policy_sha256") != expected_policy_sha
        or gate.get("runtime") != expected_runtime
        or gate.get("runtime_sha256") != expected_runtime_sha
        or not _is_lower_sha256(gate.get("binding_sha256"))
        or gate.get("draft", {}).get("sha256") != draft_record["sha256"]
        or gate.get("draft", {}).get("bytes") != draft_record["bytes"]
        or gate.get("verify", {}).get("sha256") != verify_record["sha256"]
        or gate.get("verify", {}).get("bytes") != verify_record["bytes"]
        or draft_record["path"] == verify_record["path"]
        or draft_record["sha256"] == verify_record["sha256"]
        or postprocess.get("policy_id") != spatial.POLICY_ID
        or postprocess.get("policy_sha256") != expected_policy_sha
        or postprocess.get("runtime") != expected_runtime
        or postprocess.get("experimental") is not True
        or postprocess.get("quality_claim") is not False
        or postprocess.get("input_decode_reused") is not True
        or postprocess.get("input") != {"sha256": draft_record["sha256"]}
        or postprocess.get("encoding")
        != {"compression_level": 0, "format": "PNG", "optimize": False}
        or postprocess.get("operators") != expected_policy["transform"]
        or not isinstance(output, dict)
        or set(output) != {"bytes", "dimensions", "mode", "path", "sha256"}
        or output.get("bytes") != delivery_record["bytes"]
        or output.get("sha256") != delivery_record["sha256"]
        or output.get("dimensions") != list(DELIVERY_SIZE)
        or output.get("mode") != "RGBA"
        or output.get("path") != str(delivery_path.resolve(strict=True))
    ):
        raise FrontierError("spatial75 artifact binding mismatch")

    try:
        replayed_gate = spatial.gate_pngs(draft_path, verify_path).receipt()
        decoded, _ = spatial.decode_png(draft_path)
        prepared = spatial.prepare_decoded_draft(decoded)
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            raise
        raise FrontierError("spatial75 artifact replay failed") from exc
    if canonical_json(_gate_semantics(gate)) != canonical_json(
        _gate_semantics(replayed_gate)
    ):
        raise FrontierError("spatial75 gate replay mismatch")
    if (
        prepared.input_sha256 != draft_record["sha256"]
        or prepared.input_bytes != draft_record["bytes"]
        or prepared.output_sha256 != delivery_record["sha256"]
        or prepared.output_bytes != delivery_record["bytes"]
    ):
        raise FrontierError("spatial75 deterministic output replay mismatch")


def _validate_pipeline_artifact_binding(
    pipeline: Any,
    spatial_receipt: dict[str, Any],
    artifacts: dict[str, Any],
    *,
    trial: bool,
) -> None:
    common_keys = {
        "draft_snapshot",
        "draft_snapshot_conversion_s",
        "draft_snapshot_convert_and_prepare_s",
        "fused_authorization_tail_s",
        "fused_gate_publish_tail_s",
        "fused_publish_tail_s",
        "gate_tail_s",
        "prepared",
        "publish_tail_s",
        "verify_snapshot",
        "verify_snapshot_conversion_s",
    }
    trial_only = {
        "post_verify_snapshot_gate_publish_wall_s",
        "prepare_submit_to_ready_s",
        "prepare_verify_overlap_s",
        "prepare_wait_tail_s",
        "verify_endpoint_wall_s",
    }
    expected_keys = common_keys | (trial_only if trial else set())
    if not isinstance(pipeline, dict) or set(pipeline) != expected_keys:
        raise FrontierError("pipeline snapshot shape mismatch")
    _validate_snapshot_receipt(
        pipeline["draft_snapshot"],
        artifact_sha256=artifacts["draft"]["sha256"],
        artifact_bytes=artifacts["draft"]["bytes"],
    )
    _validate_snapshot_receipt(
        pipeline["verify_snapshot"],
        artifact_sha256=artifacts["verify"]["sha256"],
        artifact_bytes=artifacts["verify"]["bytes"],
    )
    prepared = pipeline.get("prepared")
    if not isinstance(prepared, dict) or set(prepared) != {
        "binding_sha256",
        "experimental",
        "input",
        "policy_id",
        "policy_sha256",
        "prepared_output",
        "publication_authorized",
        "quality_claim",
        "runtime_sha256",
        "timings_ns",
    }:
        raise FrontierError("prepared pipeline receipt shape mismatch")
    source = prepared.get("input")
    output = prepared.get("prepared_output")
    gate = spatial_receipt["gate"]
    if (
        not isinstance(source, dict)
        or set(source) != {"bytes", "decoded_identity_sha256", "mode", "sha256"}
        or not isinstance(output, dict)
        or set(output)
        != {"bytes", "dimensions", "encoded_bytes_held_in_memory", "mode", "sha256"}
        or source.get("sha256") != artifacts["draft"]["sha256"]
        or source.get("bytes") != artifacts["draft"]["bytes"]
        or source.get("decoded_identity_sha256")
        != gate.get("draft", {}).get("decoded_identity_sha256")
        or source.get("mode") != gate.get("draft", {}).get("mode")
        or output.get("sha256") != artifacts["delivery"]["sha256"]
        or output.get("bytes") != artifacts["delivery"]["bytes"]
        or output.get("dimensions") != list(DELIVERY_SIZE)
        or output.get("mode") != "RGBA"
        or output.get("encoded_bytes_held_in_memory") is not True
        or prepared.get("experimental") is not True
        or prepared.get("publication_authorized") is not False
        or prepared.get("quality_claim") is not False
        or prepared.get("policy_id") != spatial_receipt["policy"]["id"]
        or prepared.get("policy_sha256") != spatial_receipt["policy_sha256"]
        or prepared.get("runtime_sha256") != gate.get("runtime_sha256")
    ):
        raise FrontierError("prepared pipeline artifact binding mismatch")
    expected_binding = hashlib.sha256(
        canonical_json(
            {
                "kind": "cx_render_spatial75_prepared_output_v1",
                "input_sha256": source["sha256"],
                "input_decoded_identity_sha256": source[
                    "decoded_identity_sha256"
                ],
                "input_mode": source["mode"],
                "input_bytes": source["bytes"],
                "output_sha256": output["sha256"],
                "output_bytes": output["bytes"],
                "policy_sha256": prepared["policy_sha256"],
                "runtime_sha256": prepared["runtime_sha256"],
            }
        )
    ).hexdigest()
    if prepared.get("binding_sha256") != expected_binding:
        raise FrontierError("prepared pipeline binding mismatch")


def _validate_manifest_common(
    manifest: dict[str, Any],
    *,
    expected_pins: dict[str, str],
    scene_sha256: str,
    scene_name: str,
    device: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if set(manifest) != {
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
    }:
        raise FrontierError("render manifest top-level shape mismatch")
    render = manifest.get("render")
    scene = manifest.get("scene")
    artifact = manifest.get("artifact")
    if not isinstance(render, dict) or set(render) != {
        "denoising_policy",
        "device",
        "engine",
        "frame",
        "height",
        "integrator_policy",
        "pixel_filter",
        "png_compression",
        "resident_mutation",
        "resident_policy",
        "sample_offset",
        "samples",
        "seed",
        "width",
        "worker_renderer_identity",
    }:
        raise FrontierError("render manifest render shape mismatch")
    worker = render.get("worker_renderer_identity")
    if (
        manifest.get("schema_version") != 2
        or manifest.get("kind") != "cx_cycles_preview_manifest"
        or manifest.get("unit_id") != BENCHMARK_UNIT_ID
        or manifest.get("evidence") != "synthetic"
        or manifest.get("preview_only") is not True
        or manifest.get("production_ready") is not False
        or manifest.get("artifact_verified") is not False
        or manifest.get("billing_eligible") is not False
        or manifest.get("pins") != expected_pins
        or manifest.get("execution_identity_revalidation")
        != {
            "initial_content": "sha256",
            "per_render": "pre_and_post_stat_identity_plus_bundle_file_set",
        }
        or not _is_lower_sha256(manifest.get("binding_sha256"))
        or not isinstance(scene, dict)
        or set(scene)
        != {"bundle_bytes", "bundle_files", "bundle_sha256", "relative_path", "sha256"}
        or scene.get("sha256") != scene_sha256
        or scene.get("relative_path") != scene_name
        or type(scene.get("bundle_bytes")) is not int
        or scene["bundle_bytes"] <= 0
        or type(scene.get("bundle_files")) is not int
        or scene["bundle_files"] <= 0
        or not _is_lower_sha256(scene.get("bundle_sha256"))
        or not isinstance(artifact, dict)
        or set(artifact) != {"media_type", "path", "sha256"}
        or artifact.get("media_type") != "image/png"
        or render.get("engine") != "CYCLES"
        or render.get("device") != device
        or render.get("png_compression") != 0
        or render.get("pixel_filter") != {"type": "BLACKMAN_HARRIS", "width": 1.5}
        or not isinstance(worker, dict)
        or worker.get("device") != device
        or worker.get("png_compression") != 0
        or worker.get("candidate_profile") != NATIVE_CANDIDATE_PROFILE
        or worker.get("candidate_denoising_policy") != DENOISING_OFF_POLICY
        or worker.get("reference_denoising_policy") != DENOISING_OFF_POLICY
        or render.get("integrator_policy", {}).get("candidate_profile")
        != NATIVE_CANDIDATE_PROFILE
        or render.get("denoising_policy", {}).get("candidate_policy")
        != DENOISING_OFF_POLICY
    ):
        raise FrontierError("render manifest common identity mismatch")
    return worker, scene


def _validate_unique_artifact_inodes(
    records: Sequence[dict[str, Any]], output_root: Path
) -> None:
    paths: set[str] = set()
    inodes: set[tuple[int, int]] = set()
    for record in records:
        path = _artifact_path(record, output_root)
        info = path.stat()
        identity = (info.st_dev, info.st_ino)
        if record["path"] in paths or identity in inodes:
            raise FrontierError("artifact records alias one published file")
        paths.add(record["path"])
        inodes.add(identity)


def _bundle_identity(root: Path, backend: Any) -> dict[str, Any]:
    if root.is_symlink() or not root.is_dir():
        raise FrontierError("scene bundle root is unsafe")
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
        if path.is_symlink():
            raise FrontierError("scene bundle contains a symlink")
        if path.is_dir():
            continue
        if not path.is_file():
            raise FrontierError("scene bundle contains a non-regular entry")
        before = path.stat()
        digest = sha256_file(path)
        after = path.stat()
        if (
            not stat.S_ISREG(before.st_mode)
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        ):
            raise FrontierError("scene bundle changed while it was hashed")
        relative = path.relative_to(root).as_posix()
        total_bytes += before.st_size
        entries.append(
            {"path": relative, "bytes": before.st_size, "sha256": digest}
        )
    if not entries:
        raise FrontierError("scene bundle is empty")
    return {
        "bundle_bytes": total_bytes,
        "bundle_files": len(entries),
        "bundle_sha256": backend._bundle_digest(entries),
    }


def _validate_measured_semantics(
    receipt: dict[str, Any], *, output_root: Path, pins: dict[str, str]
) -> None:
    try:
        import cx_cycles_render_preview_backend as backend  # noqa: PLC0415
        import run_local_cycles_spec_benchmark as local_benchmark  # noqa: PLC0415
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            raise
        raise FrontierError("measured identity helpers could not be loaded") from exc

    frame = receipt.get("frame")
    scene = receipt.get("scene")
    renderer = receipt.get("renderer_identity")
    if (
        type(frame) is not int
        or not 0 <= frame <= 1_000_000
        or not isinstance(scene, dict)
        or set(scene) != {"path", "sha256"}
        or not _is_lower_sha256(scene.get("sha256"))
        or not isinstance(renderer, dict)
    ):
        raise FrontierError("measured frame/scene/renderer identity mismatch")
    scene_path = Path(scene["path"])
    if (
        not scene_path.is_absolute()
        or scene_path.is_symlink()
        or not scene_path.is_file()
        or str(scene_path.resolve(strict=True)) != scene["path"]
        or sha256_file(scene_path) != scene["sha256"]
    ):
        raise FrontierError("measured scene bytes do not match receipt")

    executable_raw = renderer.get("executable_path")
    if not isinstance(executable_raw, str):
        raise FrontierError("renderer executable path is missing")
    executable = Path(executable_raw)
    if (
        not executable.is_absolute()
        or executable.is_symlink()
        or not executable.is_file()
        or not os.access(executable, os.X_OK)
        or str(executable.resolve(strict=True)) != executable_raw
    ):
        raise FrontierError("renderer executable path is unsafe")
    expected_renderer = local_benchmark.blender_identity(executable)
    expected_renderer = {
        **expected_renderer,
        "executable_path": executable_raw,
        "executable_sha256": sha256_file(executable),
    }
    if canonical_json(renderer) != canonical_json(expected_renderer):
        raise FrontierError("renderer identity replay mismatch")
    if receipt.get("host") != local_benchmark.host_identity():
        raise FrontierError("host identity replay mismatch")
    if (
        receipt.get("device") not in {"GPU/METAL", "CPU"}
        or receipt.get("claim_scope") != CLAIM_SCOPE
        or receipt.get("timing_scope") != TIMING_SCOPE
        or receipt.get("execution_order") != EXECUTION_ORDER
    ):
        raise FrontierError("measured scope/device identity mismatch")
    measurement_only = receipt.get("measurement_only")
    if not isinstance(measurement_only, dict) or set(measurement_only) != {
        "different_frame_reference_rearm_s",
        "quality_v3_s",
        "quality_v3_verification_s",
    }:
        raise FrontierError("measurement-only timing shape mismatch")
    for label, value in measurement_only.items():
        _finite_positive(value, label)

    candidate = receipt["candidate"]
    reference = receipt["reference"]
    warmup_receipt = receipt["warmup"]
    trace = receipt["execution_trace"]
    trial_count = receipt["trial_count"]
    if set(candidate) != {
        "artifacts_distinct",
        "draft_samples",
        "draft_seed",
        "quality_trial_selection",
        "resolution",
        "resident_mutations",
        "sample_ranges",
        "sample_ranges_disjoint",
        "spatial75",
        "trials",
        "verify_samples",
        "verify_seed",
    }:
        raise FrontierError("measured candidate shape mismatch")
    if not isinstance(warmup_receipt, dict) or set(warmup_receipt) != {
        "charged",
        "full_candidate_lane_runs",
        "gate_pass",
        "pipeline",
        "resident_mutations",
        "seconds",
        "spatial75",
    }:
        raise FrontierError("measured warmup shape mismatch")
    if (
        warmup_receipt.get("charged") is not False
        or warmup_receipt.get("full_candidate_lane_runs") != 1
        or warmup_receipt.get("gate_pass") is not True
        or not isinstance(warmup_receipt.get("pipeline"), dict)
    ):
        raise FrontierError("measured warmup semantics mismatch")
    _finite_positive(warmup_receipt.get("seconds"), "warmup seconds")
    if not isinstance(reference, dict) or set(reference) != {
        "candidate_state_reset",
        "descriptor",
        "resident_mutation",
        "resolution",
        "sample_range",
        "samples",
        "seed",
        "single_trial_disclosed",
    }:
        raise FrontierError("measured reference shape mismatch")
    if not isinstance(trace, dict) or set(trace) != {
        "candidate_handoff",
        "candidate_reference_sample_ranges_disjoint",
        "candidate_reference_seeds_distinct",
        "commands",
        "different_frame_rearm",
        "retained_validated_png_cache_empty",
        "same_backend_session",
        "same_scene_bundle",
        "worker_command_count",
        "worker_key_sha256",
        "worker_reused",
    }:
        raise FrontierError("measured execution trace shape mismatch")

    rearm_frame = frame - 1 if frame > 0 else frame + 1
    expected_commands: list[dict[str, Any]] = [
        {"command_id": 1, "frame": frame, "phase": "draft", "mutation": "broad_v1"},
        {
            "command_id": 2,
            "frame": frame,
            "phase": "verify",
            "mutation": RESIDENT_POLICY,
        },
    ]
    for index in range(trial_count):
        expected_commands.extend(
            [
                {
                    "command_id": 3 + index * 2,
                    "frame": frame,
                    "phase": "draft",
                    "mutation": RESIDENT_POLICY,
                },
                {
                    "command_id": 4 + index * 2,
                    "frame": frame,
                    "phase": "verify",
                    "mutation": RESIDENT_POLICY,
                },
            ]
        )
    expected_commands.extend(
        [
            {
                "command_id": 3 + trial_count * 2,
                "frame": rearm_frame,
                "phase": "draft",
                "mutation": "broad_v1",
            },
            {
                "command_id": 4 + trial_count * 2,
                "frame": frame,
                "phase": "baseline",
                "mutation": "broad_v1",
            },
        ]
    )
    if (
        trace.get("commands") != expected_commands
        or trace.get("different_frame_rearm") != rearm_frame
        or not _is_lower_sha256(trace.get("worker_key_sha256"))
        or warmup_receipt.get("resident_mutations") != expected_commands[:2]
        or candidate.get("resident_mutations") != expected_commands[2:-2]
    ):
        raise FrontierError("measured command/mutation trace mismatch")

    reset = reference.get("candidate_state_reset")
    if (
        not isinstance(reset, dict)
        or set(reset)
        != {
            "different_frame_rearm",
            "rearm_mutation",
            "rearm_resolution",
            "rearm_sample_offset",
            "rearm_samples",
            "rearm_seed",
            "reference_mutation",
            "same_worker",
        }
        or reset.get("different_frame_rearm") != rearm_frame
        or reset.get("rearm_mutation") != "broad_v1"
        or reset.get("reference_mutation") != "broad_v1"
        or reset.get("same_worker") is not True
        or reset.get("rearm_resolution") != [64, 64]
        or reset.get("rearm_samples") != 1
        or reset.get("rearm_sample_offset") != 0
        or type(reset.get("rearm_seed")) is not int
        or reset["rearm_seed"]
        in {candidate.get("draft_seed"), candidate.get("verify_seed"), reference.get("seed")}
    ):
        raise FrontierError("reference rearm identity mismatch")

    artifacts = receipt["artifacts"]
    warmup_artifacts = artifacts["warmup"]
    trial_artifacts = artifacts["candidate_trials"]
    all_records: list[dict[str, Any]] = list(warmup_artifacts.values())
    for row in trial_artifacts:
        all_records.extend(value for key, value in row.items() if key != "trial_index")
    all_records.extend(
        artifacts[key]
        for key in (
            "baseline",
            "baseline_config",
            "baseline_manifest",
            "rearm",
            "rearm_config",
            "timing_evidence",
        )
    )
    all_records.extend(
        [receipt["quality_v3"]["artifact"], receipt["quality_v3"]["verification_artifact"]]
    )
    _validate_unique_artifact_inodes(all_records, output_root)

    _validate_render_config(
        _artifact_path(warmup_artifacts["draft_config"], output_root),
        phase="draft",
        frame=frame,
        size=LOW_SIZE,
        samples=DRAFT_SAMPLES,
        sample_offset=0,
        seed=candidate["draft_seed"],
        output=_artifact_path(warmup_artifacts["draft"], output_root),
    )
    _validate_render_config(
        _artifact_path(warmup_artifacts["verify_config"], output_root),
        phase="verify",
        frame=frame,
        size=LOW_SIZE,
        samples=VERIFY_SAMPLES,
        sample_offset=DRAFT_SAMPLES,
        seed=candidate["verify_seed"],
        output=_artifact_path(warmup_artifacts["verify"], output_root),
    )
    _validate_spatial_artifact_binding(
        warmup_receipt["spatial75"],
        warmup_artifacts,
        output_root=output_root,
        pins=pins,
    )
    _validate_pipeline_artifact_binding(
        warmup_receipt["pipeline"],
        warmup_receipt["spatial75"],
        warmup_artifacts,
        trial=False,
    )

    expected_manifest_pins = {
        "backend_sha256": pins["backend_sha256"],
        "blender_sha256": renderer["executable_sha256"],
        "child_script_sha256": backend._BLENDER_CHILD_SHA256,
        "controller_adapter_sha256": pins["controller_adapter_sha256"],
        "controller_core_sha256": pins["controller_core_sha256"],
    }
    manifest_workers: list[dict[str, Any]] = []
    manifest_scenes: list[dict[str, Any]] = []
    for index, (trial, row) in enumerate(
        zip(candidate["trials"], trial_artifacts, strict=True)
    ):
        if not isinstance(trial, dict) or set(trial) != {
            "component_s",
            "pipeline_overlap",
            "predeclared_for_quality",
            "resident_mutations",
            "spatial75",
            "spec_s",
            "trial_index",
        }:
            raise FrontierError("candidate semantic trial shape mismatch")
        _validate_render_config(
            _artifact_path(row["draft_config"], output_root),
            phase="draft",
            frame=frame,
            size=LOW_SIZE,
            samples=DRAFT_SAMPLES,
            sample_offset=0,
            seed=candidate["draft_seed"],
            output=_artifact_path(row["draft"], output_root),
        )
        _validate_render_config(
            _artifact_path(row["verify_config"], output_root),
            phase="verify",
            frame=frame,
            size=LOW_SIZE,
            samples=VERIFY_SAMPLES,
            sample_offset=DRAFT_SAMPLES,
            seed=candidate["verify_seed"],
            output=_artifact_path(row["verify"], output_root),
        )
        draft_manifest = _validate_render_manifest(
            _artifact_path(row["draft_manifest"], output_root),
            phase="draft",
            frame=frame,
            size=LOW_SIZE,
            samples=DRAFT_SAMPLES,
            sample_offset=0,
            seed=candidate["draft_seed"],
            mutation=RESIDENT_POLICY,
            artifact=_artifact_path(row["draft"], output_root),
            artifact_sha256=row["draft"]["sha256"],
            output_root=output_root,
        )
        verify_manifest = _validate_render_manifest(
            _artifact_path(row["verify_manifest"], output_root),
            phase="verify",
            frame=frame,
            size=LOW_SIZE,
            samples=VERIFY_SAMPLES,
            sample_offset=DRAFT_SAMPLES,
            seed=candidate["verify_seed"],
            mutation=RESIDENT_POLICY,
            artifact=_artifact_path(row["verify"], output_root),
            artifact_sha256=row["verify"]["sha256"],
            output_root=output_root,
        )
        for manifest in (draft_manifest, verify_manifest):
            worker, manifest_scene = _validate_manifest_common(
                manifest,
                expected_pins=expected_manifest_pins,
                scene_sha256=scene["sha256"],
                scene_name=scene_path.name,
                device=receipt["device"],
            )
            manifest_workers.append(worker)
            manifest_scenes.append(manifest_scene)
        _validate_spatial_artifact_binding(
            trial["spatial75"], row, output_root=output_root, pins=pins
        )
        _validate_pipeline_artifact_binding(
            trial["pipeline_overlap"],
            trial["spatial75"],
            row,
            trial=True,
        )
        if (
            trial.get("trial_index") != index
            or trial.get("resident_mutations")
            != expected_commands[2 + index * 2 : 4 + index * 2]
        ):
            raise FrontierError("candidate semantic trial index mismatch")
    if canonical_json(candidate["spatial75"]) != canonical_json(
        candidate["trials"][QUALITY_TRIAL_INDEX]["spatial75"]
    ):
        raise FrontierError("selected candidate spatial75 receipt mismatch")

    _validate_render_config(
        _artifact_path(artifacts["rearm_config"], output_root),
        phase="draft",
        frame=rearm_frame,
        size=(64, 64),
        samples=1,
        sample_offset=0,
        seed=reset["rearm_seed"],
        output=_artifact_path(artifacts["rearm"], output_root),
    )
    _validate_render_config(
        _artifact_path(artifacts["baseline_config"], output_root),
        phase="baseline",
        frame=frame,
        size=DELIVERY_SIZE,
        samples=REFERENCE_SAMPLES,
        sample_offset=DRAFT_SAMPLES + VERIFY_SAMPLES,
        seed=reference["seed"],
        output=_artifact_path(artifacts["baseline"], output_root),
    )
    baseline_manifest = _validate_render_manifest(
        _artifact_path(artifacts["baseline_manifest"], output_root),
        phase="baseline",
        frame=frame,
        size=DELIVERY_SIZE,
        samples=REFERENCE_SAMPLES,
        sample_offset=DRAFT_SAMPLES + VERIFY_SAMPLES,
        seed=reference["seed"],
        mutation="broad_v1",
        artifact=_artifact_path(artifacts["baseline"], output_root),
        artifact_sha256=artifacts["baseline"]["sha256"],
        output_root=output_root,
    )
    worker, manifest_scene = _validate_manifest_common(
        baseline_manifest,
        expected_pins=expected_manifest_pins,
        scene_sha256=scene["sha256"],
        scene_name=scene_path.name,
        device=receipt["device"],
    )
    manifest_workers.append(worker)
    manifest_scenes.append(manifest_scene)
    if any(
        canonical_json(value) != canonical_json(manifest_workers[0])
        for value in manifest_workers[1:]
    ) or any(
        canonical_json(value) != canonical_json(manifest_scenes[0])
        for value in manifest_scenes[1:]
    ):
        raise FrontierError("render manifests disagree on worker or scene bundle")
    expected_version = renderer.get("version", "")
    if expected_version.startswith("Blender "):
        expected_version = expected_version[len("Blender ") :]
    if manifest_workers[0].get("blender_version") != expected_version:
        raise FrontierError("worker and renderer version identities disagree")
    manifest_bundle = manifest_scenes[0]
    expected_bundle = {
        "bundle_bytes": manifest_bundle["bundle_bytes"],
        "bundle_files": manifest_bundle["bundle_files"],
        "bundle_sha256": manifest_bundle["bundle_sha256"],
    }
    source_bundle = _bundle_identity(scene_path.parent, backend)
    first_artifact_parts = PurePosixPath(
        trial_artifacts[0]["draft"]["path"]
    ).parts
    if (
        not first_artifact_parts
        or not first_artifact_parts[0].startswith("cycles-preview-")
    ):
        raise FrontierError("render session artifact path is invalid")
    private_bundle_root = (
        output_root
        / first_artifact_parts[0]
        / "scenes"
        / (
            f"bundle-{manifest_bundle['bundle_sha256']}-"
            f"{scene['sha256']}"
        )
    )
    private_scene = private_bundle_root / manifest_bundle["relative_path"]
    private_bundle = _bundle_identity(private_bundle_root, backend)
    if (
        source_bundle != expected_bundle
        or private_bundle != expected_bundle
        or not private_scene.is_file()
        or private_scene.is_symlink()
        or sha256_file(private_scene) != scene["sha256"]
    ):
        raise FrontierError("source/private scene bundle replay mismatch")
    expected_worker_key = (
        str(private_scene.resolve(strict=True)),
        scene["sha256"],
        manifest_bundle["bundle_sha256"],
        renderer["executable_sha256"],
        backend._BLENDER_CHILD_SHA256,
        canonical_json(NATIVE_CANDIDATE_PROFILE).decode("ascii"),
        canonical_json(DENOISING_OFF_POLICY).decode("ascii"),
        RESIDENT_POLICY,
        "METAL" if receipt["device"] == "GPU/METAL" else "CPU",
    )
    if trace.get("worker_key_sha256") != hashlib.sha256(
        canonical_json(list(expected_worker_key))
    ).hexdigest():
        raise FrontierError("resident worker key replay mismatch")
    expected_descriptor = {
        "schema_version": backend.ARTIFACT_SCHEMA_VERSION,
        "kind": backend.ARTIFACT_KIND,
        "manifest_path": artifacts["baseline_manifest"]["path"],
    }
    if reference.get("descriptor") != expected_descriptor:
        raise FrontierError("reference descriptor does not bind its manifest")


def validate_receipt(
    receipt: dict[str, Any], *, output_root: Path | None = None
) -> None:
    required = {
        "artifacts",
        "baseline_s",
        "candidate",
        "claim_scope",
        "device",
        "evidence",
        "execution_order",
        "execution_trace",
        "frame",
        "host",
        "kind",
        "meets_200x_verified",
        "meets_1000x_verified",
        "measurement_only",
        "pins",
        "preview_only",
        "quality_pass",
        "quality_v3",
        "receipt_trust",
        "reference",
        "reference_used_for_product_decision",
        "renderer_identity",
        "resident_policy",
        "scene",
        "schema_version",
        "spec_s",
        "speedup_x",
        "timing_scope",
        "timing_statistics",
        "trial_count",
        "variance_estimate",
        "warmup",
    }
    if not isinstance(receipt, dict) or set(receipt) != required:
        raise FrontierError("receipt shape mismatch")
    if receipt["schema_version"] != SCHEMA_VERSION or receipt["kind"] != KIND:
        raise FrontierError("receipt identity mismatch")
    if (
        receipt["receipt_trust"] != RECEIPT_TRUST
        or receipt["evidence"] not in {"measured", "synthetic"}
        or receipt["preview_only"] is not True
        or type(receipt["trial_count"]) is not int
        or not 3 <= receipt["trial_count"] <= 15
        or receipt["trial_count"] % 2 == 0
    ):
        raise FrontierError("receipt scope/trial identity mismatch")
    trusted = receipt["evidence"] == "measured"
    if trusted and output_root is None:
        raise FrontierError("measured receipt validation requires its artifact root")
    baseline = _finite_positive(receipt["baseline_s"], "baseline_s")
    product = _finite_positive(receipt["spec_s"], "spec_s")
    speedup = _finite_positive(receipt["speedup_x"], "speedup_x")
    if not math.isclose(speedup, baseline / product, rel_tol=1e-12, abs_tol=1e-12):
        raise FrontierError("speedup is inconsistent with measured times")
    timing = receipt["timing_statistics"]
    if not isinstance(timing, dict):
        raise FrontierError("timing statistics shape mismatch")
    samples = timing.get("candidate_spec_s_samples")
    if (
        not isinstance(samples, list)
        or len(samples) != receipt["trial_count"]
    ):
        raise FrontierError("candidate timing sample count mismatch")
    values = [_finite_positive(value, "candidate timing") for value in samples]
    median = statistics.median(values)
    p95 = _type7_quantile(values, 0.95)
    variance = statistics.pvariance(values)
    for observed, expected, label in (
        (timing.get("median_s"), median, "median"),
        (timing.get("p95_s_type7"), p95, "p95"),
        (timing.get("minimum_s"), min(values), "minimum"),
        (timing.get("maximum_s"), max(values), "maximum"),
        (timing.get("population_variance_s2"), variance, "variance"),
        (receipt.get("variance_estimate"), variance, "receipt variance"),
        (product, median, "headline median"),
    ):
        if (
            isinstance(observed, bool)
            or not isinstance(observed, (int, float))
            or not math.isfinite(float(observed))
            or not math.isclose(
                float(observed), float(expected), rel_tol=1e-12, abs_tol=1e-12
            )
        ):
            raise FrontierError(f"{label} timing statistic mismatch")
    if (
        timing.get("headline") != "median_candidate_wall_seconds"
        or timing.get("reference_trial_count") != 1
    ):
        raise FrontierError("timing headline/reference disclosure mismatch")
    if type(receipt["quality_pass"]) is not bool:
        raise FrontierError("quality_pass must be boolean")
    candidate = receipt.get("candidate")
    reference = receipt.get("reference")
    trace = receipt.get("execution_trace")
    if (
        not isinstance(candidate, dict)
        or not isinstance(reference, dict)
        or not isinstance(trace, dict)
    ):
        raise FrontierError("candidate/reference/trace shape mismatch")
    trials = candidate.get("trials")
    selection = candidate.get("quality_trial_selection")
    if (
        not isinstance(trials, list)
        or len(trials) != receipt["trial_count"]
        or not isinstance(selection, dict)
        or selection.get("predeclared_before_execution") is not True
        or selection.get("trial_index") != QUALITY_TRIAL_INDEX
        or selection.get("selection_rule")
        != "fixed_index_zero_not_timing_or_reference_selected"
    ):
        raise FrontierError("candidate trial selection mismatch")
    for index, (trial, sample) in enumerate(zip(trials, values, strict=True)):
        if (
            not isinstance(trial, dict)
            or trial.get("trial_index") != index
            or trial.get("predeclared_for_quality") is not (index == QUALITY_TRIAL_INDEX)
            or not math.isclose(
                _finite_positive(trial.get("spec_s"), "trial spec_s"),
                sample,
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
            or not isinstance(trial.get("pipeline_overlap"), dict)
        ):
            raise FrontierError("candidate trial receipt mismatch")
        projection = _trial_timing_projection(trial)
        if projection["trial_index"] != index:
            raise FrontierError("candidate trial timing index mismatch")
    if (
        candidate.get("resolution") != list(LOW_SIZE)
        or candidate.get("draft_samples") != DRAFT_SAMPLES
        or candidate.get("verify_samples") != VERIFY_SAMPLES
        or candidate.get("draft_seed") == candidate.get("verify_seed")
        or candidate.get("sample_ranges")
        != {
            "draft": [0, DRAFT_SAMPLES],
            "verify": [DRAFT_SAMPLES, DRAFT_SAMPLES + VERIFY_SAMPLES],
        }
        or candidate.get("sample_ranges_disjoint") is not True
        or candidate.get("artifacts_distinct") is not True
    ):
        raise FrontierError("candidate sampling identity mismatch")
    commands = trace.get("commands")
    expected_command_count = 4 + receipt["trial_count"] * 2
    if (
        trace.get("worker_reused") is not True
        or trace.get("same_backend_session") is not True
        or trace.get("same_scene_bundle") is not True
        or trace.get("worker_command_count") != expected_command_count
        or not isinstance(commands, list)
        or [row.get("command_id") for row in commands if isinstance(row, dict)]
        != list(range(1, expected_command_count + 1))
        or trace.get("candidate_reference_seeds_distinct") is not True
        or trace.get("candidate_reference_sample_ranges_disjoint") is not True
        or trace.get("retained_validated_png_cache_empty") is not True
        or trace.get("candidate_handoff")
        != "one_shot_backend_validated_png_snapshots"
    ):
        raise FrontierError("execution trace closure mismatch")
    if (
        reference.get("resolution") != list(DELIVERY_SIZE)
        or reference.get("samples") != REFERENCE_SAMPLES
        or reference.get("sample_range")
        != [
            DRAFT_SAMPLES + VERIFY_SAMPLES,
            DRAFT_SAMPLES + VERIFY_SAMPLES + REFERENCE_SAMPLES,
        ]
        or reference.get("resident_mutation") != "broad_v1"
        or reference.get("single_trial_disclosed") is not True
        or reference.get("seed")
        in {candidate.get("draft_seed"), candidate.get("verify_seed")}
    ):
        raise FrontierError("reference sampling/reset identity mismatch")
    quality_block = receipt.get("quality_v3")
    if (
        not isinstance(quality_block, dict)
        or set(quality_block)
        != {
            "artifact",
            "independent_verification",
            "measurement_only",
            "result",
            "verification_artifact",
        }
        or quality_block.get("measurement_only") is not True
    ):
        raise FrontierError("quality-v3 receipt shape mismatch")
    quality_result = quality_block.get("result")
    verification = quality_block.get("independent_verification")
    if (
        not isinstance(quality_result, dict)
        or not isinstance(verification, dict)
        or verification.get("proof_verified") is not True
        or verification.get("errors") != []
        or verification.get("quality_pass") is not quality_result.get("pass")
        or receipt["quality_pass"]
        is not (
            quality_result.get("pass") is True
            and verification.get("pass") is True
        )
        or verification.get("proof_result_sha256")
        != hashlib.sha256(canonical_json(quality_result)).hexdigest()
        or verification.get("recomputed_result_sha256")
        != hashlib.sha256(canonical_json(quality_result)).hexdigest()
    ):
        raise FrontierError("quality-v3 independent verification mismatch")
    renderer = receipt.get("renderer_identity")
    if (
        receipt.get("resident_policy") != RESIDENT_POLICY
        or not isinstance(renderer, dict)
        or (trusted and renderer.get("official_signed_executable") is not True)
        or not isinstance(renderer.get("executable_sha256"), str)
        or len(renderer["executable_sha256"]) != 64
        or any(
            character not in "0123456789abcdef"
            for character in renderer["executable_sha256"]
        )
    ):
        raise FrontierError("renderer evidence/resident policy mismatch")
    expected_200 = trusted and receipt["quality_pass"] and speedup >= 200.0
    expected_1000 = trusted and receipt["quality_pass"] and speedup >= 1000.0
    if receipt["meets_200x_verified"] is not expected_200:
        raise FrontierError("200x decision mismatch")
    if receipt["meets_1000x_verified"] is not expected_1000:
        raise FrontierError("1000x decision mismatch")
    if receipt["reference_used_for_product_decision"] is not False:
        raise FrontierError("reference cannot steer the product lane")
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, dict):
        raise FrontierError("artifact closure shape mismatch")
    expected_artifact_keys = {
        "baseline",
        "baseline_config",
        "baseline_manifest",
        "candidate_trials",
        "predeclared_quality_trial_index",
        "rearm",
        "rearm_config",
        "timing_evidence",
        "warmup",
    }
    if (
        set(artifacts) != expected_artifact_keys
        or artifacts["predeclared_quality_trial_index"] != QUALITY_TRIAL_INDEX
        or not isinstance(artifacts["candidate_trials"], list)
        or len(artifacts["candidate_trials"]) != receipt["trial_count"]
    ):
        raise FrontierError("artifact closure identity mismatch")
    warmup = artifacts.get("warmup")
    if not isinstance(warmup, dict) or set(warmup) != {
        "delivery",
        "draft",
        "draft_config",
        "verify",
        "verify_config",
    }:
        raise FrontierError("warmup artifact closure mismatch")
    for record in warmup.values():
        _validate_artifact_record(record, output_root)
    trial_artifact_keys = {
        "delivery",
        "draft",
        "draft_config",
        "draft_manifest",
        "trial_index",
        "verify",
        "verify_config",
        "verify_manifest",
    }
    for index, row in enumerate(artifacts["candidate_trials"]):
        if (
            not isinstance(row, dict)
            or set(row) != trial_artifact_keys
            or row.get("trial_index") != index
        ):
            raise FrontierError("candidate artifact trial mismatch")
        for key, record in row.items():
            if key != "trial_index":
                _validate_artifact_record(record, output_root)
    for key in (
        "baseline",
        "baseline_config",
        "baseline_manifest",
        "rearm",
        "rearm_config",
        "timing_evidence",
    ):
        _validate_artifact_record(artifacts[key], output_root)
    _validate_artifact_record(quality_block.get("artifact"), output_root)
    _validate_artifact_record(
        quality_block.get("verification_artifact"), output_root
    )
    selected = artifacts["candidate_trials"][QUALITY_TRIAL_INDEX]
    quality_inputs = quality_result.get("inputs", {})
    if (
        quality_inputs.get("target_dimensions") != list(DELIVERY_SIZE)
        or quality_inputs.get("candidate", {}).get("sha256")
        != selected["delivery"]["sha256"]
        or quality_inputs.get("candidate", {}).get("bytes")
        != selected["delivery"]["bytes"]
        or quality_inputs.get("reference", {}).get("sha256")
        != artifacts["baseline"]["sha256"]
        or quality_inputs.get("reference", {}).get("bytes")
        != artifacts["baseline"]["bytes"]
        or verification.get("artifacts", {}).get("candidate")
        != {
            "sha256": selected["delivery"]["sha256"],
            "bytes": selected["delivery"]["bytes"],
        }
        or verification.get("artifacts", {}).get("reference")
        != {
            "sha256": artifacts["baseline"]["sha256"],
            "bytes": artifacts["baseline"]["bytes"],
        }
    ):
        raise FrontierError("quality candidate/reference artifact binding mismatch")
    pins = receipt.get("pins")
    if not isinstance(pins, dict) or pins != _code_pins():
        raise FrontierError("receipt code pins are not the exact current pins")
    if output_root is not None:
        timing_path = _artifact_path(artifacts["timing_evidence"], output_root)
        stored_timing = _read_json_object(timing_path)
        expected_timing = _timing_evidence(
            baseline_s=baseline,
            trials=trials,
            pins=pins,
        )
        if canonical_json(stored_timing) != canonical_json(expected_timing):
            raise FrontierError("immutable timing evidence binding mismatch")
        proof_path = _artifact_path(quality_block["artifact"], output_root)
        verification_path = _artifact_path(
            quality_block["verification_artifact"], output_root
        )
        stored_quality = _read_json_object(proof_path)
        stored_verification = _read_json_object(verification_path)
        if (
            canonical_json(stored_quality) != canonical_json(quality_result)
            or canonical_json(stored_verification) != canonical_json(verification)
        ):
            raise FrontierError("stored quality evidence does not match receipt")
        candidate_path = _artifact_path(selected["delivery"], output_root)
        reference_path = _artifact_path(artifacts["baseline"], output_root)
        replayed = _recompute_quality_verification(
            proof_path, candidate_path, reference_path
        )
        if canonical_json(replayed) != canonical_json(stored_verification):
            raise FrontierError("independent quality-v3 replay mismatch")
        if trusted:
            _validate_measured_semantics(
                receipt,
                output_root=output_root,
                pins=pins,
            )
    canonical_json(receipt)


def run_frontier(args: argparse.Namespace) -> dict[str, Any]:
    validate_args(args)
    args.output_root.mkdir(mode=0o700)
    output_root = args.output_root.resolve(strict=True)
    json_out = args.json_out or (output_root / "receipt.json")
    scene = args.scene.resolve(strict=True)
    blender = args.blender.resolve(strict=True)
    scene_sha = sha256_file(scene)
    blender_sha = sha256_file(blender)
    starting_pins = _code_pins()

    sys.path.insert(0, str(HERE))
    backend: Any | None = None
    executor: ThreadPoolExecutor | None = None
    contexts_to_clean: list[dict[str, Any]] = []
    environment_before: dict[str, str | None] = {}
    try:
        import cx_agent_render_preview_driver as driver  # noqa: PLC0415
        import cx_render_quality_v3 as quality  # noqa: PLC0415
        import cx_render_spatial75_v1 as spatial  # noqa: PLC0415
        import run_local_cycles_spec_benchmark as local_benchmark  # noqa: PLC0415
        import verify_cx_render_quality_v3 as quality_verifier  # noqa: PLC0415

        if (
            spatial.runtime_identity().get("module_sha256")
            != starting_pins["spatial75_module_sha256"]
            or quality.runtime_identity().get("metric_module_sha256")
            != starting_pins["quality_v3_module_sha256"]
            or sha256_file(Path(quality_verifier.__file__).resolve())
            != starting_pins["quality_v3_verifier_sha256"]
        ):
            raise FrontierError("loaded spatial/quality module pin mismatch")
        executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="cx-spatial75-prepare"
        )
        executor.submit(time.perf_counter).result()

        renderer_identity = local_benchmark.blender_identity(blender)
        renderer_trusted = bool(renderer_identity["official_signed_executable"])
        if not renderer_trusted and not args.allow_untrusted_renderer:
            raise FrontierError(
                "renderer is not an official Apple-signed Blender Foundation executable"
            )
        evidence = "measured" if renderer_trusted else "synthetic"

        capability = secrets.token_hex(32)
        environment_updates = {
            "CX_SPEC_RENDER_PREVIEW_BACKEND": str(BACKEND_PATH),
            "CX_SPEC_RENDER_PREVIEW_BACKEND_SHA256": starting_pins["backend_sha256"],
            "CX_SPEC_RENDER_PREVIEW_CORE_SHA256": starting_pins[
                "controller_core_sha256"
            ],
            "CX_SPEC_RENDER_PREVIEW_ADAPTER_SHA256": starting_pins[
                "controller_adapter_sha256"
            ],
            "CX_SPEC_RENDER_CYCLES_BLENDER": str(blender),
            "CX_SPEC_RENDER_CYCLES_BLENDER_SHA256": blender_sha,
            "CX_SPEC_RENDER_CYCLES_SCENE_ROOT": str(scene.parent),
            "CX_SPEC_RENDER_CYCLES_OUTPUT_ROOT": str(output_root),
            "CX_SPEC_RENDER_CYCLES_TIMEOUT_SECS": str(args.timeout_secs),
            "CX_SPEC_RENDER_CYCLES_DEVICE": args.device,
            "CX_SPEC_RENDER_CYCLES_LOCAL_PROCESS_GROUP": "1",
            CANDIDATE_PROFILE_ENV: CANDIDATE_PROFILE,
            CANDIDATE_PROFILE_SCOPE_ENV: CANDIDATE_PROFILE_BENCHMARK_SCOPE,
            CANDIDATE_PROFILE_AUTH_ENV: capability,
            RESIDENT_POLICY_ENV: RESIDENT_POLICY,
        }
        environment_before = {
            key: os.environ.get(key) for key in environment_updates
        }
        os.environ.update(environment_updates)

        core, _adapter, loaded_core_sha, loaded_adapter_sha = (
            driver._load_pinned_controllers()
        )
        if loaded_core_sha != starting_pins["controller_core_sha256"]:
            raise FrontierError("loaded controller core pin mismatch")
        if loaded_adapter_sha != starting_pins["controller_adapter_sha256"]:
            raise FrontierError("loaded controller adapter pin mismatch")
        backend = driver._load_backend(
            BACKEND_PATH, starting_pins["backend_sha256"]
        )

        low_unit = _unit(core, scene, scene_sha, args.frame, LOW_SIZE, capability)
        full_unit = _unit(
            core, scene, scene_sha, args.frame, DELIVERY_SIZE, capability
        )
        rearm_frame = args.frame - 1 if args.frame > 0 else args.frame + 1
        rearm_unit = _unit(
            core,
            scene,
            scene_sha,
            rearm_frame,
            (64, 64),
            capability,
            draft_samples=1,
            verify_samples=1,
            repair_samples=2,
        )
        low_context = backend._context_for_unit(low_unit)
        full_context = backend._context_for_unit(full_unit)
        rearm_context = backend._context_for_unit(rearm_unit)
        contexts_to_clean = [low_context, full_context, rearm_context]

        warmup_started = time.perf_counter()
        warm_draft = low_context["unit_dir"] / "frontier-warmup-draft.png"
        warm_verify = low_context["unit_dir"] / "frontier-warmup-verify.png"
        try:
            warm_draft_sha = backend._invoke_blender(
                low_context,
                "draft",
                DRAFT_SAMPLES,
                low_context["seeds"]["draft"],
                warm_draft,
                execution_label="frontier-warmup-draft",
                retain_validated_png=True,
            )
            warm_draft_snapshot = backend._pop_retained_validated_png(
                low_context,
                phase="draft",
                path=warm_draft,
                sha256=warm_draft_sha,
            )
            warm_prepare_future = executor.submit(
                _prepare_draft, spatial, warm_draft_snapshot
            )
            warm_verify_sha = backend._invoke_blender(
                low_context,
                "verify",
                VERIFY_SAMPLES,
                low_context["seeds"]["verify"],
                warm_verify,
                execution_label="frontier-warmup-verify",
                retain_validated_png=True,
            )
            warm_verify_snapshot = backend._pop_retained_validated_png(
                low_context,
                phase="verify",
                path=warm_verify,
                sha256=warm_verify_sha,
            )
            warm_verify_decode_started = time.perf_counter()
            warm_decoded_verify = _decoded_from_backend_snapshot(
                spatial, warm_verify_snapshot
            )
            warm_verify_decode_s = (
                time.perf_counter() - warm_verify_decode_started
            )
            warm_delivery = (
                low_context["unit_dir"] / "frontier-warmup-delivery.png"
            )
            warm_prepared_bundle = warm_prepare_future.result()
            warm_spatial, warm_pipeline = _pipelined_spatial_result(
                spatial,
                warm_prepared_bundle,
                warm_decoded_verify,
                warm_delivery,
            )
            warm_pipeline.update(
                {
                    "verify_snapshot": _snapshot_receipt(
                        warm_verify_snapshot
                    ),
                    "verify_snapshot_conversion_s": warm_verify_decode_s,
                }
            )
        finally:
            backend._discard_retained_validated_pngs(low_context)
        _assert_no_retained_pngs(backend, contexts_to_clean)
        if not warm_spatial.gate.passed:
            raise FrontierError("warmup draft/verify gate rejected")
        warmup_s = time.perf_counter() - warmup_started

        candidate_trials: list[dict[str, Any]] = []
        for trial_index in range(args.candidate_trials):
            prefix = f"frontier-trial-{trial_index:02d}"
            product_started = time.perf_counter()
            try:
                (
                    draft_path,
                    draft_manifest,
                    draft_sha,
                    draft_s,
                    draft_snapshot,
                ) = _render_with_manifest(
                    backend,
                    low_context,
                    "draft",
                    DRAFT_SAMPLES,
                    low_context["seeds"]["draft"],
                    f"{prefix}-draft.png",
                    f"{prefix}-draft-manifest.json",
                    f"{prefix}-draft",
                    retain_validated_png=True,
                )
                if draft_snapshot is None:
                    raise FrontierError("draft retained snapshot is unavailable")
                prepare_submitted = time.perf_counter()
                prepare_future = executor.submit(
                    _prepare_draft, spatial, draft_snapshot
                )
                verify_started = time.perf_counter()
                (
                    verify_path,
                    verify_manifest,
                    verify_sha,
                    verify_s,
                    verify_snapshot,
                ) = _render_with_manifest(
                    backend,
                    low_context,
                    "verify",
                    VERIFY_SAMPLES,
                    low_context["seeds"]["verify"],
                    f"{prefix}-verify.png",
                    f"{prefix}-verify-manifest.json",
                    f"{prefix}-verify",
                    retain_validated_png=True,
                )
                verify_finished = time.perf_counter()
                if verify_snapshot is None:
                    raise FrontierError("verify retained snapshot is unavailable")
                spatial_started = time.perf_counter()
                verify_snapshot_conversion_started = time.perf_counter()
                decoded_verify = _decoded_from_backend_snapshot(
                    spatial, verify_snapshot
                )
                verify_snapshot_conversion_s = (
                    time.perf_counter() - verify_snapshot_conversion_started
                )
                delivery_path = (
                    low_context["unit_dir"] / f"{prefix}-delivery.png"
                )
                prepare_wait_started = time.perf_counter()
                prepared_bundle = prepare_future.result()
                prepare_wait_s = time.perf_counter() - prepare_wait_started
                spatial_result, pipeline = _pipelined_spatial_result(
                    spatial, prepared_bundle, decoded_verify, delivery_path
                )
                spatial_s = time.perf_counter() - spatial_started
                prepare_verify_overlap_s = max(
                    0.0,
                    min(prepared_bundle["finished"], verify_finished)
                    - max(prepared_bundle["started"], verify_started),
                )
                pipeline.update(
                    {
                        "prepare_submit_to_ready_s": (
                            prepared_bundle["finished"] - prepare_submitted
                        ),
                        "prepare_verify_overlap_s": prepare_verify_overlap_s,
                        "prepare_wait_tail_s": prepare_wait_s,
                        "verify_endpoint_wall_s": (
                            verify_finished - verify_started
                        ),
                        "verify_snapshot": _snapshot_receipt(
                            verify_snapshot
                        ),
                        "verify_snapshot_conversion_s": (
                            verify_snapshot_conversion_s
                        ),
                        "post_verify_snapshot_gate_publish_wall_s": spatial_s,
                    }
                )
            finally:
                backend._discard_retained_validated_pngs(low_context)
            _assert_no_retained_pngs(backend, contexts_to_clean)
            trial_spec_s = time.perf_counter() - product_started
            if not spatial_result.gate.passed:
                raise FrontierError(
                    f"candidate trial {trial_index} draft/verify gate rejected"
                )
            if draft_sha == verify_sha:
                raise FrontierError(
                    f"candidate trial {trial_index} independent artifacts are byte-identical"
                )
            if spatial_result.postprocess.input_sha256 != draft_sha:
                raise FrontierError(
                    f"candidate trial {trial_index} did not select its bound draft"
                )
            candidate_trials.append(
                {
                    "trial_index": trial_index,
                    "draft_path": draft_path,
                    "draft_manifest": draft_manifest,
                    "draft_sha256": draft_sha,
                    "verify_path": verify_path,
                    "verify_manifest": verify_manifest,
                    "verify_sha256": verify_sha,
                    "delivery_path": delivery_path,
                    "spatial_result": spatial_result,
                    "draft_s": draft_s,
                    "verify_s": verify_s,
                    "spatial_s": spatial_s,
                    "pipeline": pipeline,
                    "spec_s": trial_spec_s,
                }
            )
        selected_trial = candidate_trials[QUALITY_TRIAL_INDEX]
        draft_path = selected_trial["draft_path"]
        verify_path = selected_trial["verify_path"]
        delivery_path = selected_trial["delivery_path"]
        draft_sha = selected_trial["draft_sha256"]
        verify_sha = selected_trial["verify_sha256"]
        spatial_result = selected_trial["spatial_result"]

        rearm_path = rearm_context["unit_dir"] / "frontier-reference-rearm.png"
        rearm_started = time.perf_counter()
        backend._invoke_blender(
            rearm_context,
            "draft",
            1,
            rearm_context["seeds"]["draft"],
            rearm_path,
            execution_label="frontier-reference-rearm",
        )
        rearm_s = time.perf_counter() - rearm_started
        rearm_mutation = rearm_context["resident_mutation_history"][-1]["mutation"]
        if rearm_mutation != backend.RESIDENT_POLICY_BROAD:
            raise FrontierError("different-frame reference rearm was not broad")

        baseline_started = time.perf_counter()
        baseline_descriptor = backend.baseline(full_unit)
        baseline_s = time.perf_counter() - baseline_started
        baseline_path = full_context["unit_dir"] / "baseline.png"
        baseline_manifest = full_context["unit_dir"] / "baseline-manifest.json"
        baseline_mutation = full_context["resident_mutation_history"][-1]["mutation"]
        if baseline_mutation != backend.RESIDENT_POLICY_BROAD:
            raise FrontierError("4096-SPP reference did not use broad resident mutation")
        _assert_no_retained_pngs(backend, contexts_to_clean)

        execution_trace = _validate_execution_trace(
            backend,
            low_context,
            rearm_context,
            full_context,
            frame=args.frame,
            rearm_frame=rearm_frame,
            capability=capability,
            candidate_trials=args.candidate_trials,
        )
        _validate_render_config(
            low_context["unit_dir"] / "frontier-warmup-draft-render-config.json",
            phase="draft",
            frame=args.frame,
            size=LOW_SIZE,
            samples=DRAFT_SAMPLES,
            sample_offset=0,
            seed=low_context["seeds"]["draft"],
            output=warm_draft,
        )
        _validate_render_config(
            low_context["unit_dir"] / "frontier-warmup-verify-render-config.json",
            phase="verify",
            frame=args.frame,
            size=LOW_SIZE,
            samples=VERIFY_SAMPLES,
            sample_offset=DRAFT_SAMPLES,
            seed=low_context["seeds"]["verify"],
            output=warm_verify,
        )
        for trial in candidate_trials:
            prefix = f"frontier-trial-{trial['trial_index']:02d}"
            _validate_render_config(
                low_context["unit_dir"] / f"{prefix}-draft-render-config.json",
                phase="draft",
                frame=args.frame,
                size=LOW_SIZE,
                samples=DRAFT_SAMPLES,
                sample_offset=0,
                seed=low_context["seeds"]["draft"],
                output=trial["draft_path"],
            )
            _validate_render_config(
                low_context["unit_dir"] / f"{prefix}-verify-render-config.json",
                phase="verify",
                frame=args.frame,
                size=LOW_SIZE,
                samples=VERIFY_SAMPLES,
                sample_offset=DRAFT_SAMPLES,
                seed=low_context["seeds"]["verify"],
                output=trial["verify_path"],
            )
            _validate_render_manifest(
                trial["draft_manifest"],
                phase="draft",
                frame=args.frame,
                size=LOW_SIZE,
                samples=DRAFT_SAMPLES,
                sample_offset=0,
                seed=low_context["seeds"]["draft"],
                mutation=RESIDENT_POLICY,
                artifact=trial["draft_path"],
                artifact_sha256=trial["draft_sha256"],
                output_root=output_root,
            )
            _validate_render_manifest(
                trial["verify_manifest"],
                phase="verify",
                frame=args.frame,
                size=LOW_SIZE,
                samples=VERIFY_SAMPLES,
                sample_offset=DRAFT_SAMPLES,
                seed=low_context["seeds"]["verify"],
                mutation=RESIDENT_POLICY,
                artifact=trial["verify_path"],
                artifact_sha256=trial["verify_sha256"],
                output_root=output_root,
            )
        _validate_render_config(
            rearm_context["unit_dir"] / "frontier-reference-rearm-render-config.json",
            phase="draft",
            frame=rearm_frame,
            size=(64, 64),
            samples=1,
            sample_offset=0,
            seed=rearm_context["seeds"]["draft"],
            output=rearm_path,
        )
        _validate_render_config(
            full_context["unit_dir"] / "baseline-render-config.json",
            phase="baseline",
            frame=args.frame,
            size=DELIVERY_SIZE,
            samples=REFERENCE_SAMPLES,
            sample_offset=DRAFT_SAMPLES + VERIFY_SAMPLES,
            seed=full_context["seeds"]["baseline"],
            output=baseline_path,
        )
        baseline_sha = sha256_file(baseline_path)
        _validate_render_manifest(
            baseline_manifest,
            phase="baseline",
            frame=args.frame,
            size=DELIVERY_SIZE,
            samples=REFERENCE_SAMPLES,
            sample_offset=DRAFT_SAMPLES + VERIFY_SAMPLES,
            seed=full_context["seeds"]["baseline"],
            mutation=backend.RESIDENT_POLICY_BROAD,
            artifact=baseline_path,
            artifact_sha256=baseline_sha,
            output_root=output_root,
        )
        expected_baseline_descriptor = {
            "schema_version": backend.ARTIFACT_SCHEMA_VERSION,
            "kind": backend.ARTIFACT_KIND,
            "manifest_path": baseline_manifest.resolve(strict=True)
            .relative_to(output_root)
            .as_posix(),
        }
        if baseline_descriptor != expected_baseline_descriptor:
            raise FrontierError("baseline descriptor did not bind its published manifest")

        quality_started = time.perf_counter()
        quality_result = quality.evaluate_pngs(
            delivery_path, baseline_path, target_size=DELIVERY_SIZE
        )
        quality_s = time.perf_counter() - quality_started
        quality_path = output_root / "quality-v3.json"
        _write_new_json(quality_path, quality_result)
        quality_verification_started = time.perf_counter()
        quality_verification = quality_verifier.verify_paths(
            quality_path, delivery_path, baseline_path
        )
        quality_verification_s = (
            time.perf_counter() - quality_verification_started
        )
        if (
            quality_verification.get("proof_verified") is not True
            or quality_verification.get("quality_pass")
            is not quality_result.get("pass")
            or quality_verification.get("errors") != []
        ):
            raise FrontierError("independent quality-v3 verification failed")
        quality_verification_path = output_root / "quality-v3-verification.json"
        _write_new_json(quality_verification_path, quality_verification)

        ending_pins = _code_pins()
        if ending_pins != starting_pins:
            raise FrontierError("pinned code changed during benchmark")
        renderer_identity_after = local_benchmark.blender_identity(blender)
        for key in (
            "version_output_sha256",
            "runtime_bundle",
            "apple_signature_valid",
            "official_signed_executable",
        ):
            if renderer_identity_after.get(key) != renderer_identity.get(key):
                raise FrontierError("pinned Blender identity changed during benchmark")
        if sha256_file(blender) != blender_sha or sha256_file(scene) != scene_sha:
            raise FrontierError("pinned Blender executable or scene changed during benchmark")

        baseline_s = _finite_positive(baseline_s, "baseline_s")
        spec_samples = [
            _finite_positive(trial["spec_s"], "candidate trial spec_s")
            for trial in candidate_trials
        ]
        spec_s = _finite_positive(statistics.median(spec_samples), "median spec_s")
        spec_p95 = _type7_quantile(spec_samples, 0.95)
        spec_variance = statistics.pvariance(spec_samples)
        speedup = baseline_s / spec_s
        quality_pass = bool(
            quality_result.get("pass") is True
            and quality_verification.get("pass") is True
        )
        measured = evidence == "measured"
        spatial_receipt = spatial_result.receipt()
        quality_record = _relative_record(quality_path, output_root)
        quality_verification_record = _relative_record(
            quality_verification_path, output_root
        )
        candidate_history = list(
            low_context.get("resident_mutation_history", ())
        )
        warmup_mutations = candidate_history[:2]
        measured_candidate_mutations = candidate_history[2:]
        if len(measured_candidate_mutations) != args.candidate_trials * 2:
            raise FrontierError("candidate mutation trace length mismatch")
        trial_receipts: list[dict[str, Any]] = []
        trial_artifacts: list[dict[str, Any]] = []
        for trial in candidate_trials:
            trial_index = trial["trial_index"]
            mutations = measured_candidate_mutations[
                trial_index * 2 : trial_index * 2 + 2
            ]
            if [row.get("mutation") for row in mutations] != [
                RESIDENT_POLICY,
                RESIDENT_POLICY,
            ]:
                raise FrontierError(
                    f"candidate trial {trial_index} did not use minimal mutations"
                )
            trial_spatial = trial["spatial_result"].receipt()
            if (
                trial_spatial["runtime"].get("module_sha256")
                != starting_pins["spatial75_module_sha256"]
                or trial_spatial["gate"]["draft"]["sha256"]
                != trial["draft_sha256"]
                or trial_spatial["gate"]["verify"]["sha256"]
                != trial["verify_sha256"]
                or trial_spatial["postprocess"]["input"]["sha256"]
                != trial["draft_sha256"]
            ):
                raise FrontierError(
                    f"candidate trial {trial_index} spatial receipt mismatch"
                )
            artifacts = {
                "draft": _relative_record(trial["draft_path"], output_root),
                "draft_config": _relative_record(
                    low_context["unit_dir"]
                    / f"frontier-trial-{trial_index:02d}-draft-render-config.json",
                    output_root,
                ),
                "draft_manifest": _relative_record(
                    trial["draft_manifest"], output_root
                ),
                "verify": _relative_record(trial["verify_path"], output_root),
                "verify_config": _relative_record(
                    low_context["unit_dir"]
                    / f"frontier-trial-{trial_index:02d}-verify-render-config.json",
                    output_root,
                ),
                "verify_manifest": _relative_record(
                    trial["verify_manifest"], output_root
                ),
                "delivery": _relative_record(
                    trial["delivery_path"], output_root
                ),
            }
            if (
                trial_spatial["postprocess"]["output"]["sha256"]
                != artifacts["delivery"]["sha256"]
            ):
                raise FrontierError(
                    f"candidate trial {trial_index} delivery digest mismatch"
                )
            _validate_snapshot_receipt(
                trial["pipeline"]["draft_snapshot"],
                artifact_sha256=artifacts["draft"]["sha256"],
                artifact_bytes=artifacts["draft"]["bytes"],
            )
            _validate_snapshot_receipt(
                trial["pipeline"]["verify_snapshot"],
                artifact_sha256=artifacts["verify"]["sha256"],
                artifact_bytes=artifacts["verify"]["bytes"],
            )
            trial_artifacts.append(
                {"trial_index": trial_index, **artifacts}
            )
            trial_receipts.append(
                {
                    "trial_index": trial_index,
                    "predeclared_for_quality": trial_index == QUALITY_TRIAL_INDEX,
                    "spec_s": trial["spec_s"],
                    "component_s": {
                        "draft_endpoint_and_manifest": trial["draft_s"],
                        "verify_endpoint_and_manifest": trial["verify_s"],
                        "post_verify_wait_gate_and_publish": trial["spatial_s"],
                    },
                    "pipeline_overlap": trial["pipeline"],
                    "resident_mutations": mutations,
                    "spatial75": trial_spatial,
                }
            )
        warm_spatial_receipt = warm_spatial.receipt()
        warm_artifacts = {
            "draft": _relative_record(warm_draft, output_root),
            "draft_config": _relative_record(
                low_context["unit_dir"]
                / "frontier-warmup-draft-render-config.json",
                output_root,
            ),
            "verify": _relative_record(warm_verify, output_root),
            "verify_config": _relative_record(
                low_context["unit_dir"]
                / "frontier-warmup-verify-render-config.json",
                output_root,
            ),
            "delivery": _relative_record(warm_delivery, output_root),
        }
        if (
            warm_spatial_receipt["gate"]["draft"]["sha256"]
            != warm_artifacts["draft"]["sha256"]
            or warm_spatial_receipt["gate"]["verify"]["sha256"]
            != warm_artifacts["verify"]["sha256"]
            or warm_spatial_receipt["postprocess"]["output"]["sha256"]
            != warm_artifacts["delivery"]["sha256"]
        ):
            raise FrontierError("warmup spatial receipt mismatch")
        _validate_snapshot_receipt(
            warm_pipeline["draft_snapshot"],
            artifact_sha256=warm_artifacts["draft"]["sha256"],
            artifact_bytes=warm_artifacts["draft"]["bytes"],
        )
        _validate_snapshot_receipt(
            warm_pipeline["verify_snapshot"],
            artifact_sha256=warm_artifacts["verify"]["sha256"],
            artifact_bytes=warm_artifacts["verify"]["bytes"],
        )
        receipt_pins = starting_pins
        renderer_identity = {
            **renderer_identity,
            "executable_path": str(blender),
            "executable_sha256": blender_sha,
        }
        timing_evidence_path = output_root / "frontier-timing-evidence.json"
        _write_new_json(
            timing_evidence_path,
            _timing_evidence(
                baseline_s=baseline_s,
                trials=trial_receipts,
                pins=receipt_pins,
            ),
        )
        timing_evidence_record = _relative_record(
            timing_evidence_path, output_root
        )
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "evidence": evidence,
            "receipt_trust": RECEIPT_TRUST,
            "claim_scope": CLAIM_SCOPE,
            "timing_scope": TIMING_SCOPE,
            "preview_only": True,
            "trial_count": args.candidate_trials,
            "variance_estimate": spec_variance,
            "timing_statistics": {
                "headline": "median_candidate_wall_seconds",
                "candidate_spec_s_samples": spec_samples,
                "median_s": spec_s,
                "p95_s_type7": spec_p95,
                "minimum_s": min(spec_samples),
                "maximum_s": max(spec_samples),
                "population_variance_s2": spec_variance,
                "reference_trial_count": 1,
            },
            "execution_order": list(EXECUTION_ORDER),
            "execution_trace": execution_trace,
            "reference_used_for_product_decision": False,
            "measurement_only": {
                "different_frame_reference_rearm_s": rearm_s,
                "quality_v3_s": quality_s,
                "quality_v3_verification_s": quality_verification_s,
            },
            "device": (
                "GPU/METAL"
                if renderer_trusted and args.device == "METAL"
                else ("CPU" if renderer_trusted else f"UNTRUSTED/{args.device}")
            ),
            "resident_policy": RESIDENT_POLICY,
            "scene": {
                "path": str(scene),
                "sha256": scene_sha,
            },
            "frame": args.frame,
            "warmup": {
                "charged": False,
                "full_candidate_lane_runs": 1,
                "seconds": warmup_s,
                "gate_pass": warm_spatial.gate.passed,
                "resident_mutations": warmup_mutations,
                "pipeline": warm_pipeline,
                "spatial75": warm_spatial_receipt,
            },
            "candidate": {
                "resolution": list(LOW_SIZE),
                "draft_samples": DRAFT_SAMPLES,
                "verify_samples": VERIFY_SAMPLES,
                "draft_seed": low_context["seeds"]["draft"],
                "verify_seed": low_context["seeds"]["verify"],
                "sample_ranges": {
                    "draft": [0, DRAFT_SAMPLES],
                    "verify": [DRAFT_SAMPLES, DRAFT_SAMPLES + VERIFY_SAMPLES],
                },
                "sample_ranges_disjoint": True,
                "artifacts_distinct": draft_sha != verify_sha,
                "resident_mutations": measured_candidate_mutations,
                "quality_trial_selection": {
                    "predeclared_before_execution": True,
                    "trial_index": QUALITY_TRIAL_INDEX,
                    "selection_rule": "fixed_index_zero_not_timing_or_reference_selected",
                },
                "trials": trial_receipts,
                "spatial75": spatial_receipt,
            },
            "reference": {
                "resolution": list(DELIVERY_SIZE),
                "samples": REFERENCE_SAMPLES,
                "seed": full_context["seeds"]["baseline"],
                "sample_range": [
                    DRAFT_SAMPLES + VERIFY_SAMPLES,
                    DRAFT_SAMPLES + VERIFY_SAMPLES + REFERENCE_SAMPLES,
                ],
                "resident_mutation": baseline_mutation,
                "descriptor": baseline_descriptor,
                "single_trial_disclosed": True,
                "candidate_state_reset": {
                    "same_worker": execution_trace["worker_reused"],
                    "different_frame_rearm": rearm_frame,
                    "rearm_mutation": rearm_mutation,
                    "reference_mutation": baseline_mutation,
                    "rearm_resolution": [64, 64],
                    "rearm_samples": 1,
                    "rearm_sample_offset": 0,
                    "rearm_seed": rearm_context["seeds"]["draft"],
                },
            },
            "baseline_s": baseline_s,
            "spec_s": spec_s,
            "speedup_x": speedup,
            "quality_pass": quality_pass,
            "meets_200x_verified": measured and quality_pass and speedup >= 200.0,
            "meets_1000x_verified": measured and quality_pass and speedup >= 1000.0,
            "quality_v3": {
                "measurement_only": True,
                "result": quality_result,
                "artifact": quality_record,
                "independent_verification": quality_verification,
                "verification_artifact": quality_verification_record,
            },
            "artifacts": {
                "warmup": warm_artifacts,
                "candidate_trials": trial_artifacts,
                "predeclared_quality_trial_index": QUALITY_TRIAL_INDEX,
                "baseline": _relative_record(baseline_path, output_root),
                "baseline_config": _relative_record(
                    full_context["unit_dir"] / "baseline-render-config.json",
                    output_root,
                ),
                "baseline_manifest": _relative_record(baseline_manifest, output_root),
                "rearm": _relative_record(rearm_path, output_root),
                "rearm_config": _relative_record(
                    rearm_context["unit_dir"]
                    / "frontier-reference-rearm-render-config.json",
                    output_root,
                ),
                "timing_evidence": timing_evidence_record,
            },
            "renderer_identity": renderer_identity,
            "host": local_benchmark.host_identity(),
            "pins": receipt_pins,
        }
        validate_receipt(receipt, output_root=output_root)
        if (
            _code_pins() != starting_pins
            or sha256_file(blender) != blender_sha
            or sha256_file(scene) != scene_sha
        ):
            raise FrontierError("pinned inputs changed before receipt publication")
        _write_new_json(json_out, receipt)
        return receipt
    finally:
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
        if backend is not None:
            for context in contexts_to_clean:
                backend._discard_retained_validated_pngs(context)
            backend._shutdown_worker()
        if environment_before:
            _restore_environment(environment_before)
        if sys.path and sys.path[0] == str(HERE):
            sys.path.pop(0)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        receipt = run_frontier(args)
    except Exception as exc:  # noqa: BLE001 - one fail-closed CLI envelope
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": f"{KIND}_error",
                    "error": f"{type(exc).__name__}: {exc}",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(receipt, sort_keys=True, allow_nan=False))
    return 0 if receipt["quality_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
