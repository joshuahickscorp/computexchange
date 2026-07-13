#!/usr/bin/env python3
"""Measure the resident render-spec path against a fresh same-session Cycles baseline.

This is a benchmark-only Apple/CPU harness. It deliberately does not alter the
preview command's non-billable contract: the high-SPP reference is benchmark-only
and never steers the reference-free draft/verify/repair product decision.
Both lanes use the same pinned Blender executable, scene, device, dimensions,
frame and resident worker. No historical reference cache is read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import subprocess
import sys
import tempfile
import time
from typing import Any


HERE = Path(__file__).resolve().parent
DRIVER_PATH = HERE / "cx_agent_render_preview_driver.py"
BACKEND_PATH = HERE / "cx_cycles_render_preview_backend.py"
CORE_PATH = HERE / "cx_speculative_core.py"
ADAPTER_PATH = HERE / "cx_render_spec_adapter.py"
DEFAULT_BLENDER = Path("/Applications/Blender.app/Contents/MacOS/Blender")
MAX_PIXELS = 4_194_304
CANDIDATE_PROFILE_ENV = "CX_SPEC_RENDER_CYCLES_CANDIDATE_PROFILE"
CANDIDATE_PROFILE_SCOPE_ENV = (
    "CX_SPEC_RENDER_CYCLES_CANDIDATE_PROFILE_SCOPE"
)
CANDIDATE_PROFILE_AUTH_ENV = "CX_SPEC_RENDER_CYCLES_CANDIDATE_PROFILE_AUTH"
RESIDENT_POLICY_ENV = "CX_SPEC_RENDER_CYCLES_RESIDENT_POLICY"
CANDIDATE_PROFILE_BENCHMARK_SCOPE = "benchmark_screen_v1"
BENCHMARK_PROFILE_META_KEY = "cx_benchmark_profile_auth_v1"
RESIDENT_POLICY_NAMES = ("broad_v1", "same_frame_minimal_v1")
CANDIDATE_PROFILE_NAMES = (
    "native",
    "cap16_v1",
    "cap12_v1",
    "cap8_v1",
    "cap8_lighttree_v1",
    "cap8_adaptive_v1",
    "cap8_both_v1",
    "cap8_both_relaxed_v1",
    "oidn_native_v1",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _restore_environment(snapshot: dict[str, str | None]) -> None:
    for key, value in snapshot.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _positive_int(raw: str) -> int:
    value = int(raw, 10)
    if value <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", required=True, type=Path, help="absolute .blend path")
    parser.add_argument("--blender", type=Path, default=DEFAULT_BLENDER)
    parser.add_argument(
        "--allow-untrusted-renderer",
        action="store_true",
        help="run a fixture/non-official executable, forcing synthetic evidence",
    )
    parser.add_argument("--device", choices=("CPU", "METAL"), default="METAL")
    parser.add_argument(
        "--candidate-profile",
        choices=CANDIDATE_PROFILE_NAMES,
        default="native",
        help=(
            "operator-pinned draft/verify integrator/denoising profile; repair "
            "and the measurement baseline use the fixed denoise-off scene-open "
            "reference policy; "
            "non-native choices are benchmark-screen-only"
        ),
    )
    parser.add_argument(
        "--resident-policy",
        choices=RESIDENT_POLICY_NAMES,
        default="broad_v1",
        help=(
            "operator-pinned resident scene mutation policy; "
            "same_frame_minimal_v1 is private benchmark-screen-only"
        ),
    )
    parser.add_argument("--width", type=_positive_int, default=1920)
    parser.add_argument("--height", type=_positive_int, default=1080)
    parser.add_argument("--frame", type=int, default=1)
    parser.add_argument("--reference-samples", type=_positive_int, default=4096)
    parser.add_argument("--draft-samples", type=_positive_int, default=16)
    parser.add_argument("--verify-samples", type=_positive_int, default=16)
    parser.add_argument("--timeout-secs", type=_positive_int, default=600)
    parser.add_argument(
        "--output-root",
        type=Path,
        help="existing/private artifact parent (default: a new /tmp directory)",
    )
    parser.add_argument("--json-out", type=Path, help="also write the final receipt JSON")
    parser.add_argument(
        "--force-json-out",
        action="store_true",
        help="allow replacing --json-out if it already exists",
    )
    return parser.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    if not args.scene.is_absolute():
        raise ValueError("--scene must be absolute")
    if not args.blender.is_absolute():
        raise ValueError("--blender must be absolute")
    if args.scene.suffix != ".blend":
        raise ValueError("--scene must end in lowercase .blend")
    if not args.scene.is_file():
        raise ValueError(f"scene is not a regular file: {args.scene}")
    if not args.blender.is_file() or not os.access(args.blender, os.X_OK):
        raise ValueError(f"Blender is not executable: {args.blender}")
    if not 16 <= args.width <= 4096 or not 16 <= args.height <= 4096:
        raise ValueError("width and height must each be in [16,4096]")
    if args.width * args.height > MAX_PIXELS:
        raise ValueError(f"width*height exceeds {MAX_PIXELS}")
    if not 0 <= args.frame <= 1_000_000:
        raise ValueError("frame must be in [0,1000000]")
    if not 1 <= args.draft_samples <= 64:
        raise ValueError("draft samples must be in [1,64]")
    if not 1 <= args.verify_samples <= 64:
        raise ValueError("verify samples must be in [1,64]")
    if not 2 <= args.reference_samples <= 4096:
        raise ValueError("reference samples must be in [2,4096]")
    if args.reference_samples <= max(args.draft_samples, args.verify_samples):
        raise ValueError("reference samples must exceed both low-SPP sample counts")
    if not 1 <= args.timeout_secs <= 600:
        raise ValueError("timeout must be in [1,600]")


def _command_value(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = (completed.stdout or "").strip().splitlines()
    return value[0] if completed.returncode == 0 and value else None


def _runtime_bundle_fingerprint(blender: Path) -> dict[str, Any] | None:
    app_root = next(
        (candidate for candidate in (blender, *blender.parents) if candidate.suffix == ".app"),
        None,
    )
    if app_root is None:
        return None
    entries: list[tuple[str, int, str]] = []
    for path in sorted(app_root.rglob("*"), key=lambda value: value.as_posix()):
        relative = path.relative_to(app_root)
        if path.is_symlink():
            raise ValueError("Blender runtime bundle contains an unsupported symlink")
        if not path.is_file():
            continue
        size = path.stat().st_size
        entries.append((relative.as_posix(), size, sha256_file(path)))
    digest = hashlib.sha256()
    total_bytes = 0
    for relative, size, file_sha in entries:
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(size.to_bytes(8, "big"))
        digest.update(bytes.fromhex(file_sha))
        total_bytes += size
    return {
        "root": str(app_root),
        "sha256": digest.hexdigest(),
        "files": len(entries),
        "bytes": total_bytes,
        "all_regular_files_included": True,
    }


def blender_identity(blender: Path) -> dict[str, Any]:
    """Bind the Apple benchmark to a Foundation-signed executable and full bundle."""
    version_environment = os.environ.copy()
    version_environment["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        version_run = subprocess.run(
            [str(blender), "--version"],
            env=version_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        version_run = None
    version_lines = (
        (version_run.stdout or "").strip().splitlines()
        if version_run is not None and version_run.returncode == 0
        else []
    )
    version = version_lines[0] if version_lines else None
    identity: dict[str, Any] = {
        "version": version,
        "version_output_sha256": (
            hashlib.sha256((version_run.stdout or "").encode("utf-8")).hexdigest()
            if version_run is not None and version_run.returncode == 0
            else None
        ),
        "apple_signature_checked": platform.system() == "Darwin",
        "apple_signature_valid": False,
        "apple_bundle_seal_valid": False,
        "bundle_identifier": None,
        "team_identifier": None,
        "authorities": [],
        "official_signed_executable": False,
        "runtime_bundle": _runtime_bundle_fingerprint(blender),
        "trust_scope": (
            "Apple-signed Blender Foundation executable plus a local full-bundle "
            "fingerprint; the benchmark receipt remains local_unattested"
        ),
    }
    if platform.system() != "Darwin":
        return identity
    try:
        verified = subprocess.run(
            [
                "/usr/bin/codesign",
                "--verify",
                "--strict",
                "--ignore-resources",
                "--verbose=2",
                str(blender),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            check=False,
        )
        described = subprocess.run(
            ["/usr/bin/codesign", "-dv", "--verbose=4", str(blender)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            check=False,
        )
        sealed = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--strict", "--verbose=2", str(blender)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return identity
    signature_lines = ((described.stdout or "") + (described.stderr or "")).splitlines()
    values: dict[str, str] = {}
    authorities: list[str] = []
    for line in signature_lines:
        if line.startswith("Authority="):
            authorities.append(line.split("=", 1)[1])
        elif "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    identity.update(
        {
            "apple_signature_valid": verified.returncode == 0 and described.returncode == 0,
            "apple_bundle_seal_valid": sealed.returncode == 0,
            "bundle_identifier": values.get("Identifier"),
            "team_identifier": values.get("TeamIdentifier"),
            "authorities": authorities,
        }
    )
    identity["official_signed_executable"] = bool(
        version is not None
        and version.startswith("Blender ")
        and identity["apple_signature_valid"]
        and identity["bundle_identifier"] == "org.blenderfoundation.blender"
        and identity["team_identifier"] == "68UA947AUU"
        and any("Stichting Blender Foundation" in value for value in authorities)
    )
    return identity


def host_identity() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_brand": _command_value(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"]),
        "memory_bytes": _command_value(["/usr/sbin/sysctl", "-n", "hw.memsize"]),
    }


def _preview_experiment_meets(
    *,
    renderer_trusted: bool,
    canonical_receipt: dict[str, Any],
    audit: dict[str, Any],
    speedup: float | None,
    minimum: float,
) -> bool:
    return bool(
        renderer_trusted
        and canonical_receipt.get("quality_gate") is True
        and audit.get("passed") is True
        and audit.get("sample_ranges_disjoint") is True
        and isinstance(audit.get("candidate"), dict)
        and audit["candidate"].get("phase") == "draft"
        and speedup is not None
        and speedup >= minimum
    )


def _write_json(path: Path, value: dict[str, Any], *, force: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(16)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if force:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError as exc:
                raise FileExistsError(
                    f"refusing to replace existing receipt: {path}"
                ) from exc
            temporary.unlink()
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.close(fd)
        temporary.unlink(missing_ok=True)


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    validate_args(args)
    scene = args.scene.resolve(strict=True)
    blender = args.blender.resolve(strict=True)
    renderer_identity = blender_identity(blender)
    renderer_trusted = bool(renderer_identity["official_signed_executable"])
    if not renderer_trusted and not args.allow_untrusted_renderer:
        raise ValueError(
            "renderer is not an official Apple-signed Blender Foundation binary; "
            "use --allow-untrusted-renderer only for synthetic fixtures"
        )
    evidence = "measured" if renderer_trusted else "synthetic"
    if args.output_root is None:
        output_root = Path(tempfile.mkdtemp(prefix="cx-cycles-spec-benchmark-"))
    else:
        output_root = args.output_root.resolve()
        output_root.mkdir(mode=0o700, parents=True, exist_ok=True)

    backend_sha = sha256_file(BACKEND_PATH)
    driver_sha = sha256_file(DRIVER_PATH)
    core_sha = sha256_file(CORE_PATH)
    adapter_sha = sha256_file(ADAPTER_PATH)
    harness_sha = sha256_file(Path(__file__).resolve())
    blender_sha = sha256_file(blender)
    scene_sha = sha256_file(scene)
    private_benchmark_policy = (
        args.candidate_profile != "native"
        or args.resident_policy != "broad_v1"
    )
    candidate_profile_auth = (
        secrets.token_hex(32) if private_benchmark_policy else ""
    )
    environment_updates = {
        "CX_SPEC_RENDER_PREVIEW_BACKEND": str(BACKEND_PATH),
        "CX_SPEC_RENDER_PREVIEW_BACKEND_SHA256": backend_sha,
        "CX_SPEC_RENDER_PREVIEW_CORE_SHA256": core_sha,
        "CX_SPEC_RENDER_PREVIEW_ADAPTER_SHA256": adapter_sha,
        "CX_SPEC_RENDER_CYCLES_BLENDER": str(blender),
        "CX_SPEC_RENDER_CYCLES_BLENDER_SHA256": blender_sha,
        "CX_SPEC_RENDER_CYCLES_SCENE_ROOT": str(scene.parent),
        "CX_SPEC_RENDER_CYCLES_OUTPUT_ROOT": str(output_root),
        "CX_SPEC_RENDER_CYCLES_TIMEOUT_SECS": str(args.timeout_secs),
        "CX_SPEC_RENDER_CYCLES_DEVICE": args.device,
        "CX_SPEC_RENDER_CYCLES_LOCAL_PROCESS_GROUP": "1",
        CANDIDATE_PROFILE_ENV: args.candidate_profile,
        CANDIDATE_PROFILE_SCOPE_ENV: CANDIDATE_PROFILE_BENCHMARK_SCOPE,
        CANDIDATE_PROFILE_AUTH_ENV: candidate_profile_auth,
        RESIDENT_POLICY_ENV: args.resident_policy,
    }
    environment_before = {
        key: os.environ.get(key) for key in environment_updates
    }
    os.environ.update(environment_updates)
    path_inserted = False
    try:
        sys.path.insert(0, str(HERE))
        path_inserted = True
        import cx_agent_render_preview_driver as driver  # noqa: PLC0415

        core, render_adapter, loaded_core_sha, loaded_adapter_sha = (
            driver._load_pinned_controllers()
        )
        backend = driver._load_backend(BACKEND_PATH, backend_sha)
        payload = {
            "scene_path": scene.name,
            "scene_sha256": scene_sha,
            "width": args.width,
            "height": args.height,
            "frame": args.frame,
            "draft_samples": args.draft_samples,
            "verify_samples": args.verify_samples,
            "repair_samples": args.reference_samples,
        }
        unit = core.SpecUnit(
            "local-metal-benchmark",
            "render",
            payload,
            (
                {BENCHMARK_PROFILE_META_KEY: candidate_profile_auth}
                if private_benchmark_policy
                else {}
            ),
        )
        context = backend._context_for_unit(unit)
    except BaseException:
        if path_inserted and sys.path and sys.path[0] == str(HERE):
            sys.path.pop(0)
        _restore_environment(environment_before)
        raise
    warmup_s = 0.0
    try:
        # Prime the entire candidate path before either measured lane. This
        # covers Blender/Metal startup, scene/BVH/material residency, both
        # disjoint low-SPP dispatches, PNG I/O and the Pillow agreement kernel.
        # It prevents the always-first high-SPP baseline from being the only
        # thing that warms candidate-visible state.
        warmup_start = time.perf_counter()
        warmup_draft = context["unit_dir"] / "benchmark-warmup-draft.png"
        warmup_verify = context["unit_dir"] / "benchmark-warmup-verify.png"
        backend._invoke_blender(
            context,
            "draft",
            args.draft_samples,
            context["seeds"]["draft"],
            warmup_draft,
            execution_label="benchmark-warmup-draft",
        )
        backend._invoke_blender(
            context,
            "verify",
            args.verify_samples,
            context["seeds"]["verify"],
            warmup_verify,
            execution_label="benchmark-warmup-verify",
        )
        backend._agreement(
            warmup_draft, warmup_verify, (args.width, args.height)
        )
        warmup_s = time.perf_counter() - warmup_start

        adapter = render_adapter.RenderSpecAdapter(
            tier=render_adapter.QualityTier(
                global_min=backend.GLOBAL_AGREEMENT_MIN,
                worst_tile_min=backend.WORST_TILE_AGREEMENT_MIN,
                canonical_tier="preview",
            ),
            branch_id="local-cycles-spec-benchmark-v1",
        )
        engine = adapter.build_engine(
            draft=backend.draft,
            verify=backend.verify,
            repair=backend.repair,
            baseline=backend.baseline,
            benchmark_equal=backend.benchmark_equal,
            evidence=evidence,
        )
        wall_start = time.perf_counter()
        outputs, raw_receipt = engine.run(
            [unit],
            meta={
                "benchmark": "fresh-same-session-cycles-baseline",
                "resident_policy": args.resident_policy,
                "reference_used_for_product_decision": False,
                "cache_used": False,
            },
            measure_baseline=True,
        )
        benchmark_wall_s = time.perf_counter() - wall_start
        audit = context.get("benchmark_audit")
        if not isinstance(audit, dict):
            raise RuntimeError("benchmark comparator did not emit its audit")
        renderer_identity_after = blender_identity(blender)
        if (
            renderer_identity_after.get("version_output_sha256")
            != renderer_identity.get("version_output_sha256")
            or renderer_identity_after.get("runtime_bundle") != renderer_identity.get("runtime_bundle")
            or renderer_identity_after.get("apple_signature_valid")
            != renderer_identity.get("apple_signature_valid")
        ):
            raise RuntimeError("pinned Blender runtime identity changed during benchmark")
        ending_code_pins = {
            "benchmark_harness_sha256": sha256_file(Path(__file__).resolve()),
            "render_preview_driver_sha256": sha256_file(DRIVER_PATH),
            "backend_sha256": sha256_file(BACKEND_PATH),
            "controller_core_sha256": sha256_file(CORE_PATH),
            "controller_adapter_sha256": sha256_file(ADAPTER_PATH),
        }
        starting_code_pins = {
            "benchmark_harness_sha256": harness_sha,
            "render_preview_driver_sha256": driver_sha,
            "backend_sha256": backend_sha,
            "controller_core_sha256": core_sha,
            "controller_adapter_sha256": adapter_sha,
        }
        if ending_code_pins != starting_code_pins:
            raise RuntimeError("pinned benchmark code changed during execution")
        canonical_receipt = adapter.from_speculative_receipt(
            raw_receipt,
            evidence=(
                render_adapter.MEASURED
                if renderer_trusted
                else render_adapter.SYNTHETIC
            ),
        ).to_dict()
        speedup = canonical_receipt["speedup_vs_baseline"]
        result = {
            "schema_version": 1,
            "kind": "cx_local_cycles_spec_benchmark",
            "evidence": evidence,
            "receipt_trust": "local_unattested",
            "claim_scope": (
                (
                    "single-frame resident steady-state preview ratio; fresh high-SPP "
                    "Cycles baseline and reference-free spec path on the same pinned session"
                )
                if renderer_trusted
                else "synthetic fixture timing; no Blender/Cycles speed claim"
            ),
            "timing_scope": "resident_steady_state_after_uncharged_warmup",
            "cold_start_included": False,
            "trial_count": 1,
            "variance_estimate": None,
            "timing_statistics": "fixed-order single trial; no variance estimate",
            "execution_order": [
                "uncharged_full_candidate_warmup",
                "measured_baseline",
                "measured_candidate",
                "measurement_only_baseline_audit",
            ],
            "order_bias_control": "full candidate path warmed before baseline",
            "cache_used": False,
            "execution_identity_revalidation": (
                "initial_sha256_plus_pre_and_post_stat_identity_and_bundle_file_set"
            ),
            "reference_used_for_product_decision": False,
            "preview_only": True,
            "production_ready": False,
            "device": (
                "GPU/METAL" if renderer_trusted and args.device == "METAL"
                else ("CPU" if renderer_trusted else f"UNTRUSTED/{args.device}")
            ),
            "renderer_identity": renderer_identity,
            "worker_renderer_identity": context.get("worker_renderer_identity"),
            "resident_policy": args.resident_policy,
            "scene": str(scene),
            "scene_sha256": scene_sha,
            "resolution": [args.width, args.height],
            "frame": args.frame,
            "reference_samples": args.reference_samples,
            "draft_samples": args.draft_samples,
            "verify_samples": args.verify_samples,
            "sample_ranges_disjoint": bool(audit["sample_ranges_disjoint"]),
            "sample_ranges": audit["sample_ranges"],
            "warmup_candidate_runs": 1,
            "warmup_s_uncharged": round(warmup_s, 6),
            "benchmark_wall_s": round(benchmark_wall_s, 6),
            "baseline_s": canonical_receipt["baseline_total_time_s"],
            "spec_s": canonical_receipt["total_product_time_s"],
            "speedup_x": speedup,
            "quality_gate": bool(audit["passed"]),
            "quality_metric": audit["metric"],
            "quality_contract": (
                "8-bit display-RGB mean-absolute agreement over a resolution-relative "
                "regional grid plus a fixed-scale catastrophic microtile sentinel; "
                "not SSIM or perceptual equivalence"
            ),
            "global_agreement": audit["global_agreement"],
            "worst_tile_agreement": audit["worst_tile_agreement"],
            "meets_50x_preview_experiment": _preview_experiment_meets(
                renderer_trusted=renderer_trusted,
                canonical_receipt=canonical_receipt,
                audit=audit,
                speedup=speedup,
                minimum=50,
            ),
            "meets_100x_preview_experiment": _preview_experiment_meets(
                renderer_trusted=renderer_trusted,
                canonical_receipt=canonical_receipt,
                audit=audit,
                speedup=speedup,
                minimum=100,
            ),
            "benchmark_audit": audit,
            "controller_receipt": canonical_receipt,
            "outputs": outputs,
            "artifact_root": str(output_root),
            "host": host_identity(),
            "pins": {
                "benchmark_harness_sha256": harness_sha,
                "render_preview_driver_sha256": driver_sha,
                "blender_sha256": blender_sha,
                "backend_sha256": backend_sha,
                "controller_core_sha256": loaded_core_sha,
                "controller_adapter_sha256": loaded_adapter_sha,
            },
        }
        if args.json_out is not None:
            _write_json(args.json_out, result, force=args.force_json_out)
        return result
    finally:
        try:
            backend._shutdown_worker()
        finally:
            if path_inserted and sys.path and sys.path[0] == str(HERE):
                sys.path.pop(0)
            _restore_environment(environment_before)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        result = run_benchmark(args)
    except Exception as exc:  # noqa: BLE001 - one closed CLI error envelope
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "cx_local_cycles_spec_benchmark_error",
                    "error": f"{type(exc).__name__}: {exc}",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
