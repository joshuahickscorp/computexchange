#!/usr/bin/env python3
"""Build the pinned Cycles-compatible Stylized Levi deformation derivative.

Run this script inside Blender 4.2.1 with ``--background --python ... --``.
The official 2015 source remains immutable.  This derivative binds its dormant
two-pose armature action, replaces unsupported legacy material nodes with a
small Principled compatibility graph, and removes one unused external image
pointer that would otherwise escape the pinned scene bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import bpy


EXPECTED_SOURCE_SHA256 = (
    "5c0a4a6f282483d159431ebe71c9c249ecbb18a2f95ed3abf05d7fa083a72e80"
)
EXPECTED_SLOT_MATERIALS = {
    "basic_mat",
    "belt",
    "belt_small",
    "boots",
    "duster",
    "eye",
    "fabric_1",
    "hair",
    "jacket",
    "logo_back",
    "logo_blue",
    "logo_white",
    "logo_wire",
    "metal",
    "pants",
    "rubber",
    "skin",
    "wood",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    if "--" not in __import__("sys").argv:
        raise ValueError("missing Blender script argument separator")
    raw = __import__("sys").argv[__import__("sys").argv.index("--") + 1 :]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument(
        "--expected-source-sha256", default=EXPECTED_SOURCE_SHA256
    )
    return parser.parse_args(raw)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def slot_material_names() -> set[str]:
    return {
        slot.material.name
        for obj in bpy.data.objects
        for slot in obj.material_slots
        if slot.material is not None
    }


def pose_snapshot(scene: bpy.types.Scene, rig: bpy.types.Object, frame: int):
    scene.frame_set(frame)
    bpy.context.view_layer.update()
    rows: dict[str, tuple[float, ...]] = {}
    for bone in rig.pose.bones:
        rows[bone.name] = tuple(
            round(float(value), 9)
            for matrix_row in bone.matrix
            for value in matrix_row
        )
    encoded = json.dumps(
        rows, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return rows, hashlib.sha256(encoded).hexdigest()


def base_color_for(
    material: bpy.types.Material,
    original_colors: dict[str, tuple[float, float, float, float]],
) -> tuple[float, float, float, float]:
    aliases = {
        "boots": "boots_d.001",
    }
    source_name = aliases.get(material.name, f"{material.name}_d")
    return original_colors.get(source_name, original_colors[material.name])


def replace_material_graphs() -> list[dict[str, object]]:
    original_colors = {
        material.name: tuple(float(value) for value in material.diffuse_color)
        for material in bpy.data.materials
    }
    converted: list[dict[str, object]] = []
    eye_image = bpy.data.images.get("eye_d")
    require(eye_image is not None and bool(eye_image.packed_file), "packed eye image missing")

    for material in sorted(bpy.data.materials, key=lambda item: item.name):
        color = base_color_for(material, original_colors)
        material.diffuse_color = color
        material.use_nodes = True
        tree = material.node_tree
        require(tree is not None, f"material {material.name} has no node tree")
        tree.nodes.clear()
        output = tree.nodes.new("ShaderNodeOutputMaterial")
        shader = tree.nodes.new("ShaderNodeBsdfPrincipled")
        shader.inputs["Base Color"].default_value = color
        shader.inputs["Roughness"].default_value = (
            0.25
            if material.name in {"eye", "metal"}
            else 0.7
            if material.name in {"hair", "rubber"}
            else 0.5
        )
        shader.inputs["Metallic"].default_value = (
            0.8 if material.name == "metal" else 0.0
        )
        tree.links.new(shader.outputs["BSDF"], output.inputs["Surface"])
        if material.name == "eye":
            texture = tree.nodes.new("ShaderNodeTexImage")
            texture.image = eye_image
            tree.links.new(texture.outputs["Color"], shader.inputs["Base Color"])
        converted.append(
            {
                "name": material.name,
                "base_color": [round(value, 9) for value in color],
                "object_slot_material": material.name in EXPECTED_SLOT_MATERIALS,
            }
        )

    for group in list(bpy.data.node_groups):
        require(group.users == 0, f"legacy node group still has users: {group.name}")
        bpy.data.node_groups.remove(group)
    return converted


def main() -> None:
    args = parse_args()
    source = args.source.resolve(strict=True)
    destination = args.destination.expanduser().resolve(strict=False)
    require(source.is_file() and source.suffix == ".blend", "source must be a .blend file")
    require(destination.is_absolute(), "destination must be absolute")
    require(destination.suffix == ".blend", "destination must end in .blend")
    require(destination.parent.is_dir(), "destination parent must already exist")
    require(not destination.exists(), "refusing to replace an existing derivative")
    require(source != destination, "source and destination must differ")
    source_sha = sha256_file(source)
    require(source_sha == args.expected_source_sha256, "source SHA-256 mismatch")

    bpy.ops.wm.open_mainfile(filepath=str(source), load_ui=False)
    require(Path(bpy.data.filepath).resolve(strict=True) == source, "Blender opened wrong source")
    require(len(bpy.data.scenes) == 1, "expected exactly one scene")
    scene = bpy.context.scene
    require(scene.name == "Scene", "unexpected scene name")
    require(scene.render.engine == "BLENDER_EEVEE_NEXT", "unexpected source engine")
    require(scene.camera is not None and scene.camera.name == "Camera", "camera mismatch")
    close_camera = bpy.data.objects.get("Camera.001")
    require(
        close_camera is not None and close_camera.type == "CAMERA",
        "close deformation camera missing",
    )
    require(slot_material_names() == EXPECTED_SLOT_MATERIALS, "material-slot set mismatch")

    rig = bpy.data.objects.get("male_metarig")
    action = bpy.data.actions.get("pose_test")
    require(rig is not None and rig.type == "ARMATURE", "male_metarig missing")
    require(action is not None, "pose_test action missing")
    require(len(action.fcurves) == 397, "pose_test f-curve count mismatch")
    require(tuple(action.frame_range) == (6.0, 7.0), "pose_test frame range mismatch")
    armature_modifiers = [
        modifier
        for obj in bpy.data.objects
        for modifier in obj.modifiers
        if modifier.type == "ARMATURE"
    ]
    lattice_modifiers = [
        modifier
        for obj in bpy.data.objects
        for modifier in obj.modifiers
        if modifier.type == "LATTICE"
    ]
    require(len(armature_modifiers) == 41, "armature modifier count mismatch")
    require(len(lattice_modifiers) == 6, "lattice modifier count mismatch")
    require(
        all(modifier.object == rig for modifier in armature_modifiers),
        "armature modifier targets differ",
    )

    rig.animation_data_create()
    rig.animation_data.action = action
    pose_6, pose_sha_6 = pose_snapshot(scene, rig, 6)
    pose_7, pose_sha_7 = pose_snapshot(scene, rig, 7)
    changed_bones = 0
    max_pose_element_delta = 0.0
    for bone_name in sorted(pose_6):
        deltas = [
            abs(left - right)
            for left, right in zip(pose_6[bone_name], pose_7[bone_name], strict=True)
        ]
        if any(delta > 1e-7 for delta in deltas):
            changed_bones += 1
        max_pose_element_delta = max(max_pose_element_delta, *deltas)
    require(pose_sha_6 != pose_sha_7, "pose frames are identical")
    require(changed_bones > 0, "pose action changes no bones")
    require(math.isfinite(max_pose_element_delta), "pose delta is non-finite")

    missing = bpy.data.images.get("levi_francke.png")
    require(missing is not None, "expected external reference image missing")
    require(missing.users == 0 and not missing.packed_file, "external image is not unused")
    missing_path = missing.filepath
    bpy.data.images.remove(missing)
    converted = replace_material_graphs()

    scene.render.engine = "CYCLES"
    scene.camera = close_camera
    scene.frame_start = 6
    scene.frame_end = 7
    scene.cycles.use_denoising = False
    scene.cycles.use_preview_denoising = False
    scene.render.use_compositing = False
    scene.render.use_sequencer = False
    scene.render.use_freestyle = False
    scene.frame_set(6)
    bpy.context.view_layer.update()
    require(
        all(node.bl_idname != "NodeUndefined" for material in bpy.data.materials for node in material.node_tree.nodes),
        "undefined material node survived conversion",
    )
    require(
        all(
            not image.filepath
            or bool(image.packed_file)
            or image.source != "FILE"
            for image in bpy.data.images
        ),
        "unpacked external image survived conversion",
    )

    bpy.ops.wm.save_as_mainfile(filepath=str(destination), check_existing=False)
    require(destination.is_file(), "Blender did not create derivative")
    require(sha256_file(source) == source_sha, "source changed while deriving")
    derivative_sha = sha256_file(destination)
    audit = {
        "schema_version": 1,
        "kind": "cx_stylized_levi_deformation_derivative",
        "source": {
            "path": str(source),
            "bytes": source.stat().st_size,
            "sha256": source_sha,
            "unchanged_after_build": True,
        },
        "derivative": {
            "path": str(destination),
            "bytes": destination.stat().st_size,
            "sha256": derivative_sha,
            "engine": scene.render.engine,
            "camera": scene.camera.name,
            "frames": [scene.frame_start, scene.frame_end],
        },
        "deformation": {
            "rig": rig.name,
            "action": action.name,
            "action_fcurves": len(action.fcurves),
            "armature_modifiers": len(armature_modifiers),
            "lattice_modifiers": len(lattice_modifiers),
            "pose_frame_6_sha256": pose_sha_6,
            "pose_frame_7_sha256": pose_sha_7,
            "changed_pose_bones": changed_bones,
            "max_pose_matrix_element_delta": round(max_pose_element_delta, 9),
        },
        "compatibility_changes": {
            "converted_materials": converted,
            "removed_unused_external_image": {
                "name": "levi_francke.png",
                "original_path": missing_path,
                "users": 0,
            },
            "remaining_node_groups": len(bpy.data.node_groups),
            "remaining_undefined_material_nodes": 0,
        },
        "claim_scope": (
            "separately hashed Cycles compatibility/deformation derivative; "
            "not appearance-equivalent to the original EEVEE-era material graph"
        ),
    }
    print("CX_DERIVATIVE_JSON=" + json.dumps(audit, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
