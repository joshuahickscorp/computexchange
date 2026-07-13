#!/usr/bin/env python3
"""Short resident follow-up for compositor handoff, EEVEE, and Workbench.

This new-file-only experiment follows the initial Koro in-memory endpoint
screen.  It uses a temporary in-memory Render Layers -> Viewer compositor link
because Blender's background multilayer ``Render Result`` does not expose a
loadable ``Image.pixels`` buffer.  It measures three Cycles-s4 Viewer handoffs
and seven alternating still/Viewer trials for warm resident EEVEE Next s4 and
Workbench.  The source blend is never saved or modified on disk.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
from typing import Any, Callable, Sequence


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import screen_blender_inmemory_endpoints as base  # noqa: E402


KIND = "cx_blender_resident_raster_followup"
CHILD_KIND = "cx_blender_resident_raster_followup_child"
SCHEMA_VERSION = 1
CHILD_REPORT = "followup-child.json"
FINAL_REPORT = "resident-raster-followup.json"
TIMING_TRIALS = 7
CYCLES_VIEWER_TRIALS = 3


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--initial-report", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--blender", type=Path, default=base.DEFAULT_BLENDER)
    parser.add_argument("--device", choices=("METAL", "CPU"), default="METAL")
    parser.add_argument("--frame", type=int, default=base.FRAME)
    parser.add_argument("--timeout-secs", type=int, default=600)
    return parser.parse_args(argv)


def _args(argv: Sequence[str] | None) -> list[str]:
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--" in raw:
        raw = raw[raw.index("--") + 1 :]
    return raw


def _validate(args: argparse.Namespace, *, child: bool) -> None:
    for name in ("scene", "reference", "initial_report"):
        path = getattr(args, name)
        if not path.is_absolute():
            raise base.ScreenError(f"--{name.replace('_', '-')} must be absolute")
        base._regular_identity(path)
    if args.scene.suffix != ".blend":
        raise base.ScreenError("scene must be a lowercase .blend")
    if not args.output_root.is_absolute():
        raise base.ScreenError("output root must be absolute")
    if not child and (args.output_root.exists() or args.output_root.is_symlink()):
        raise base.ScreenError("output root already exists")
    if not child and (
        not args.blender.is_absolute() or not os.access(args.blender, os.X_OK)
    ):
        raise base.ScreenError("Blender must be an absolute executable")
    if not 0 <= args.frame <= 1_000_000:
        raise base.ScreenError("frame is outside the bound")
    if not 1 <= args.timeout_secs <= 600:
        raise base.ScreenError("timeout is outside the bound")


def _summary(records: list[dict[str, Any]], key: str) -> dict[str, Any]:
    values = [
        float(record["timings_s"][key])
        for record in records
        if record.get("supported") is True
        and isinstance(record.get("timings_s"), dict)
        and isinstance(record["timings_s"].get(key), (int, float))
    ]
    if not values:
        return {"count": 0, "median_s": None, "min_s": None, "max_s": None}
    return {
        "count": len(values),
        "max_s": base._round9(max(values)),
        "median_s": base._round9(statistics.median(values)),
        "min_s": base._round9(min(values)),
    }


class _ViewerCapture:
    def __init__(self, bpy: Any, scene: Any):
        self.bpy = bpy
        self.scene = scene
        self.previous_use_nodes = bool(scene.use_nodes)
        self.previous_compositing = bool(scene.render.use_compositing)
        self.render_node = None
        self.viewer_node = None

    def __enter__(self) -> "_ViewerCapture":
        self.scene.use_nodes = True
        self.scene.render.use_compositing = True
        tree = self.scene.node_tree
        if tree is None:
            raise base.ScreenError("compositor node tree is unavailable")
        self.render_node = tree.nodes.new("CompositorNodeRLayers")
        self.render_node.layer = self.scene.view_layers[0].name
        self.viewer_node = tree.nodes.new("CompositorNodeViewer")
        tree.links.new(
            self.render_node.outputs["Image"], self.viewer_node.inputs["Image"]
        )
        return self

    def image(self) -> Any:
        candidates = [
            image
            for image in self.bpy.data.images
            if str(getattr(image, "type", "")) == "COMPOSITING"
            and tuple(image.size) == (base.WIDTH, base.HEIGHT)
        ]
        if len(candidates) != 1:
            raise base.ScreenError(
                f"expected one loadable compositor Viewer image, got {len(candidates)}"
            )
        return candidates[0]

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        tree = self.scene.node_tree
        if tree is not None:
            for node in (self.viewer_node, self.render_node):
                if node is not None and node.id_data == tree:
                    tree.nodes.remove(node)
        self.scene.render.use_compositing = self.previous_compositing
        self.scene.use_nodes = self.previous_use_nodes


def _viewer_trial(
    bpy: Any,
    np: Any,
    scene: Any,
    viewer: _ViewerCapture,
    output_root: Path,
    *,
    arm: str,
    trial: int,
) -> dict[str, Any]:
    destination = base._safe_output_path(output_root, f"{arm}-t{trial}.png")
    total_started = time.perf_counter()
    update_started = time.perf_counter()
    bpy.context.view_layer.update()
    update_s = time.perf_counter() - update_started
    render_started = time.perf_counter()
    bpy.ops.render.render(write_still=False)
    render_s = time.perf_counter() - render_started
    extract_started = time.perf_counter()
    image = viewer.image()
    expected = base.WIDTH * base.HEIGHT * 4
    pixels = np.empty(expected, dtype=np.float32)
    image.pixels.foreach_get(pixels)
    if pixels.nbytes != base.MAX_CAPTURE_BYTES or not bool(np.isfinite(pixels).all()):
        raise base.ScreenError("Viewer float handoff is malformed")
    pixel_sha = hashlib.sha256(pixels.tobytes(order="C")).hexdigest()
    extract_s = time.perf_counter() - extract_started
    handoff_s = time.perf_counter() - total_started
    encode_started = time.perf_counter()
    image.save_render(filepath=str(destination), scene=scene)
    encode_s = time.perf_counter() - encode_started
    inspect_started = time.perf_counter()
    artifact = base._png_identity(
        destination, width=base.WIDTH, height=base.HEIGHT
    )
    inspect_s = time.perf_counter() - inspect_started
    encoded_s = time.perf_counter() - total_started
    return {
        "arm": arm,
        "artifact": artifact,
        "engine": str(scene.render.engine),
        "render_result": {
            "buffer": "Compositor Viewer Node",
            "dtype": "float32",
            "finite": True,
            "nbytes": int(pixels.nbytes),
            "sha256": pixel_sha,
            "values": expected,
        },
        "supported": True,
        "trial": trial,
        "timings_s": {
            "artifact_validation_hash": base._round9(inspect_s),
            "png_encode_save_render": base._round9(encode_s),
            "render_operator_without_encode": base._round9(render_s),
            "total_usable_encoded": base._round9(encoded_s),
            "total_usable_float_handoff": base._round9(handoff_s),
            "viewer_float_extract_hash": base._round9(extract_s),
            "view_layer_update": base._round9(update_s),
        },
    }


def _still_trial(
    bpy: Any,
    scene: Any,
    output_root: Path,
    *,
    arm: str,
    trial: int,
) -> dict[str, Any]:
    destination = base._safe_output_path(output_root, f"{arm}-t{trial}.png")
    total_started = time.perf_counter()
    scene.render.filepath = str(destination)
    update_started = time.perf_counter()
    bpy.context.view_layer.update()
    update_s = time.perf_counter() - update_started
    render_started = time.perf_counter()
    bpy.ops.render.render(write_still=True)
    render_s = time.perf_counter() - render_started
    inspect_started = time.perf_counter()
    artifact = base._png_identity(
        destination, width=base.WIDTH, height=base.HEIGHT
    )
    inspect_s = time.perf_counter() - inspect_started
    return {
        "arm": arm,
        "artifact": artifact,
        "engine": str(scene.render.engine),
        "supported": True,
        "trial": trial,
        "timings_s": {
            "artifact_validation_hash": base._round9(inspect_s),
            "render_operator_including_encode": base._round9(render_s),
            "total_usable_encoded": base._round9(
                time.perf_counter() - total_started
            ),
            "view_layer_update": base._round9(update_s),
        },
    }


def _attempt(
    arm: str, trial: int, operation: Callable[[], dict[str, Any]]
) -> dict[str, Any]:
    try:
        return operation()
    except BaseException as exc:
        return {
            "arm": arm,
            "error": f"{type(exc).__name__}: {exc}"[:500],
            "supported": False,
            "trial": trial,
        }


def _warm(bpy: Any) -> float:
    started = time.perf_counter()
    bpy.context.view_layer.update()
    bpy.ops.render.render(write_still=False)
    return base._round9(time.perf_counter() - started)


def _child(args: argparse.Namespace) -> int:
    _validate(args, child=True)
    try:
        import bpy  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
    except ImportError as exc:
        raise base.ScreenError("Blender child requires bpy and NumPy") from exc
    source_identity = base._regular_identity(args.scene)
    source_sha, source_bytes = base._sha256_file(
        args.scene, maximum=base.MAX_SOURCE_BYTES
    )
    output_root = base._prepare_output_root(args.output_root)
    bpy.ops.wm.open_mainfile(filepath=str(args.scene))
    if Path(bpy.data.filepath).resolve(strict=True) != args.scene:
        raise base.ScreenError("Blender did not open the requested source")
    scene = bpy.context.scene
    base._configure_output(scene)
    device = base._configure_cycles_device(bpy, scene, args.device)
    scene.frame_set(args.frame)
    seed = int(source_sha[:8], 16) & 0x7FFFFFFF
    records: dict[str, list[dict[str, Any]]] = {
        "cycles-viewer-s4": [],
        "eevee-s4-still": [],
        "eevee-s4-viewer": [],
        "workbench-still": [],
        "workbench-viewer": [],
    }
    warmups: list[dict[str, Any]] = []

    with _ViewerCapture(bpy, scene) as viewer:
        base._configure_cycles(scene, 4, seed)
        warmups.append({"engine": "CYCLES", "wall_s": _warm(bpy)})
        for trial in range(CYCLES_VIEWER_TRIALS):
            records["cycles-viewer-s4"].append(
                _attempt(
                    "cycles-viewer-s4",
                    trial,
                    lambda trial=trial: _viewer_trial(
                        bpy,
                        np,
                        scene,
                        viewer,
                        output_root,
                        arm="cycles-viewer-s4",
                        trial=trial,
                    ),
                )
            )

        try:
            scene.render.engine = "BLENDER_EEVEE_NEXT"
            scene.eevee.taa_render_samples = 4
            warmups.append({"engine": "BLENDER_EEVEE_NEXT", "wall_s": _warm(bpy)})
            warmups.append({"engine": "BLENDER_EEVEE_NEXT", "wall_s": _warm(bpy)})
            for trial in range(TIMING_TRIALS):
                order = ("still", "viewer") if trial % 2 == 0 else ("viewer", "still")
                for mode in order:
                    arm = f"eevee-s4-{mode}"
                    operation = (
                        (lambda trial=trial, arm=arm: _still_trial(
                            bpy, scene, output_root, arm=arm, trial=trial
                        ))
                        if mode == "still"
                        else (lambda trial=trial, arm=arm: _viewer_trial(
                            bpy,
                            np,
                            scene,
                            viewer,
                            output_root,
                            arm=arm,
                            trial=trial,
                        ))
                    )
                    records[arm].append(_attempt(arm, trial, operation))
        except BaseException as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
            for arm in ("eevee-s4-still", "eevee-s4-viewer"):
                if not records[arm]:
                    records[arm].append(
                        {"arm": arm, "error": error, "supported": False, "trial": 0}
                    )

        try:
            scene.render.engine = "BLENDER_WORKBENCH"
            warmups.append({"engine": "BLENDER_WORKBENCH", "wall_s": _warm(bpy)})
            warmups.append({"engine": "BLENDER_WORKBENCH", "wall_s": _warm(bpy)})
            for trial in range(TIMING_TRIALS):
                order = ("still", "viewer") if trial % 2 == 0 else ("viewer", "still")
                for mode in order:
                    arm = f"workbench-{mode}"
                    operation = (
                        (lambda trial=trial, arm=arm: _still_trial(
                            bpy, scene, output_root, arm=arm, trial=trial
                        ))
                        if mode == "still"
                        else (lambda trial=trial, arm=arm: _viewer_trial(
                            bpy,
                            np,
                            scene,
                            viewer,
                            output_root,
                            arm=arm,
                            trial=trial,
                        ))
                    )
                    records[arm].append(_attempt(arm, trial, operation))
        except BaseException as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
            for arm in ("workbench-still", "workbench-viewer"):
                if not records[arm]:
                    records[arm].append(
                        {"arm": arm, "error": error, "supported": False, "trial": 0}
                    )

    if base._regular_identity(args.scene) != source_identity:
        raise base.ScreenError("source identity changed")
    after_sha, after_bytes = base._sha256_file(
        args.scene, maximum=base.MAX_SOURCE_BYTES
    )
    if (after_sha, after_bytes) != (source_sha, source_bytes):
        raise base.ScreenError("source bytes changed")
    summaries = {}
    for arm, rows in records.items():
        summaries[arm] = {
            "encoded": _summary(rows, "total_usable_encoded"),
            "float_handoff": _summary(rows, "total_usable_float_handoff"),
            "render_without_encode": _summary(rows, "render_operator_without_encode"),
            "trial_count": len(rows),
        }
    build_hash = bpy.app.build_hash
    if isinstance(build_hash, bytes):
        build_hash = build_hash.decode("ascii", errors="replace")
    report = {
        "blender": {"build_hash": str(build_hash), "version": bpy.app.version_string},
        "configuration": {
            "device": device,
            "frame": args.frame,
            "height": base.HEIGHT,
            "samples": 4,
            "seed": seed,
            "width": base.WIDTH,
        },
        "experimental_only": True,
        "kind": CHILD_KIND,
        "records": records,
        "schema_version": SCHEMA_VERSION,
        "source": {
            "bytes": source_bytes,
            "path": str(args.scene),
            "sha256": source_sha,
            "unchanged": True,
        },
        "summaries": summaries,
        "viewer_contract": {
            "encode": "Compositor Viewer Image.save_render after float foreach_get",
            "source": "temporary Render Layers Combined -> Viewer link",
            "temporary_in_memory_nodes": True,
        },
        "warmups": warmups,
    }
    base._canonical_json(report)
    base._write_new_json(output_root / CHILD_REPORT, report)
    print(json.dumps({"kind": CHILD_KIND, "ok": True}, sort_keys=True), flush=True)
    return 0


def _initial_quality_map(initial: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = {}
    for arm in initial.get("arms", []):
        if not isinstance(arm, dict):
            continue
        artifact = arm.get("artifact")
        quality = arm.get("quality_v3")
        if isinstance(artifact, dict) and isinstance(quality, dict):
            digest = artifact.get("sha256")
            if isinstance(digest, str):
                result[digest] = quality
    return result


def _host(args: argparse.Namespace) -> int:
    _validate(args, child=False)
    initial = base._load_json(args.initial_report)
    initial_quality = _initial_quality_map(initial)
    command = [
        str(args.blender),
        "--background",
        "--factory-startup",
        "--disable-autoexec",
        "--python",
        str(Path(__file__).resolve()),
        "--",
        "--child",
        "--scene",
        str(args.scene),
        "--reference",
        str(args.reference),
        "--initial-report",
        str(args.initial_report),
        "--output-root",
        str(args.output_root),
        "--blender",
        str(args.blender),
        "--device",
        args.device,
        "--frame",
        str(args.frame),
        "--timeout-secs",
        str(args.timeout_secs),
    ]
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=args.timeout_secs,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise base.ScreenError("follow-up Blender child timed out") from exc
    wall_s = time.perf_counter() - started
    if completed.returncode != 0:
        raise base.ScreenError(
            "follow-up Blender child failed: "
            + completed.stderr.decode("utf-8", errors="replace")[-4000:]
        )
    child = base._load_json(args.output_root / CHILD_REPORT)
    if child.get("kind") != CHILD_KIND:
        raise base.ScreenError("child identity mismatch")
    try:
        import cx_render_quality_v3 as quality
    except ImportError as exc:
        raise base.ScreenError("quality-v3 unavailable") from exc
    quality_cache: dict[str, dict[str, Any]] = {}
    quality_origin: dict[str, str] = {}
    # Audit at most one unmatched artifact per arm. Timing never selects it:
    # trial zero is predeclared, and exact initial hashes reuse frozen v3.
    for arm, rows in child["records"].items():
        for row in rows:
            artifact = row.get("artifact") if isinstance(row, dict) else None
            if not isinstance(artifact, dict):
                continue
            candidate = base._artifact_path(args.output_root, artifact)
            digest = artifact["sha256"]
            if digest in initial_quality:
                quality_cache[digest] = initial_quality[digest]
                quality_origin[digest] = "initial_exact_sha"
            if row.get("trial") == 0 and digest not in quality_cache:
                result = quality.evaluate_pngs(
                    candidate,
                    args.reference,
                    target_size=(base.WIDTH, base.HEIGHT),
                )
                name = f"quality-v3-{digest[:20]}.json"
                base._write_new_json(args.output_root / name, result)
                quality_cache[digest] = {
                    "full_result_path": name,
                    "summary": base._quality_summary(result),
                }
                quality_origin[digest] = "followup_trial_zero"
            row["quality"] = (
                {
                    "origin": quality_origin[digest],
                    **quality_cache[digest],
                }
                if digest in quality_cache
                else {"origin": "not_audited_timing_only"}
            )
            row["speedup_vs_4096_baseline_x"] = round(
                base.BASELINE_SECONDS
                / row["timings_s"]["total_usable_encoded"],
                6,
            )
    medians = []
    for arm, summary in child["summaries"].items():
        encoded = summary["encoded"]["median_s"]
        handoff = summary["float_handoff"]["median_s"]
        medians.append(
            {
                "arm": arm,
                "encoded_median_s": encoded,
                "encoded_speedup_x": (
                    round(base.BASELINE_SECONDS / encoded, 6) if encoded else None
                ),
                "float_handoff_median_s": handoff,
                "float_handoff_speedup_x": (
                    round(base.BASELINE_SECONDS / handoff, 6) if handoff else None
                ),
            }
        )
    encoded_rows = [row for row in medians if row["encoded_median_s"] is not None]
    encoded_rows.sort(key=lambda row: row["encoded_median_s"])
    handoff_rows = [row for row in medians if row["float_handoff_median_s"] is not None]
    handoff_rows.sort(key=lambda row: row["float_handoff_median_s"])
    report = {
        **child,
        "child_process": {
            "returncode": completed.returncode,
            "stderr_sha256": hashlib.sha256(completed.stderr).hexdigest(),
            "stdout_sha256": hashlib.sha256(completed.stdout).hexdigest(),
            "wall_s": base._round9(wall_s),
        },
        "decision": {
            "baseline_seconds": base.BASELINE_SECONDS,
            "cutoff_1000x_s": round(base.BASELINE_SECONDS / 1000.0, 9),
            "lowest_encoded": encoded_rows[0] if encoded_rows else None,
            "lowest_float_handoff": handoff_rows[0] if handoff_rows else None,
        },
        "initial_report": {
            "path": str(args.initial_report),
            "sha256": base._sha256_file(
                args.initial_report, maximum=16 * 1024 * 1024
            )[0],
        },
        "kind": KIND,
        "medians": medians,
        "quality_measurement_only": True,
    }
    base._canonical_json(report)
    base._write_new_json(args.output_root / FINAL_REPORT, report)
    print(
        json.dumps(
            {
                "decision": report["decision"],
                "kind": KIND,
                "ok": True,
                "report": str(args.output_root / FINAL_REPORT),
            },
            sort_keys=True,
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parsed = _parse_args(_args(argv))
    try:
        return _child(parsed) if parsed.child else _host(parsed)
    except BaseException as exc:
        print(
            json.dumps(
                {
                    "error": f"{type(exc).__name__}: {exc}"[:4000],
                    "kind": CHILD_KIND if parsed.child else KIND,
                    "ok": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
