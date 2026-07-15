#!/usr/bin/env python3
"""Portable generator: a REAL perforated panel with physical through-holes (Mac rear exhaust v2).

This replaces the pass-2 shader illusion (a 0.6mm cutter_box with a perforation *material*). Here the
holes are actual boolean-cut mesh openings. No absolute paths; import-safe under Blender.

build_perforated_panel(field_w_mm, field_h_mm, thickness_mm, pitch_mm, hole_d_mm, center=(x,y,z), seg)
returns (panel_object, hole_count). Uses ONE joined cutter mesh + ONE boolean DIFFERENCE for speed.
"""
import bpy, bmesh, math
from mathutils import Vector

def _mm(v): return v / 1000.0

def build_perforated_panel(field_w_mm=173.0, field_h_mm=50.0, thickness_mm=3.0,
                           pitch_mm=4.5, hole_d_mm=3.2, center=(0.0, 0.0, 0.056),
                           seg=12, name="mac-rear-perf-v2"):
    W, Hh, T = _mm(field_w_mm), _mm(field_h_mm), _mm(thickness_mm)
    p, r = _mm(pitch_mm), _mm(hole_d_mm) / 2.0
    cx, cy, cz = center
    # panel slab (thin box), thickness along Y (faces +Y/-Y like the rear face)
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(cx, cy, cz))
    panel = bpy.context.active_object; panel.name = name
    panel.scale = (W / 2.0, T / 2.0, Hh / 2.0)
    bpy.ops.object.transform_apply(scale=True)
    # hex grid of hole centers within the field (margin so holes don't clip the border)
    margin = p
    xs, zs, centers = [], [], []
    row = 0
    z = -Hh / 2.0 + margin
    dz = p * 0.866
    while z <= Hh / 2.0 - margin:
        xoff = (p / 2.0) if (row % 2) else 0.0
        x = -W / 2.0 + margin + xoff
        while x <= W / 2.0 - margin:
            centers.append((cx + x, cz + z)); x += p
        z += dz; row += 1
    # build ONE joined cutter mesh of cylinders (axis along Y), then a single boolean difference
    bm = bmesh.new()
    for (hx, hz) in centers:
        cutter = bmesh.new()
        bmesh.ops.create_cone(cutter, cap_ends=True, segments=seg, radius1=r, radius2=r, depth=T * 2.0)
        # cone default axis is Z; rotate to Y and move to (hx, cy, hz)
        rot = Vector((math.radians(90), 0, 0))
        bmesh.ops.rotate(cutter, verts=cutter.verts,
                         matrix=__import__("mathutils").Matrix.Rotation(math.radians(90), 3, 'X'))
        bmesh.ops.translate(cutter, verts=cutter.verts, vec=Vector((hx, cy, hz)))
        me = bpy.data.meshes.new("c"); cutter.to_mesh(me); cutter.free()
        for poly in me.polygons: pass
        tmp = bmesh.new(); tmp.from_mesh(me); tmp.to_mesh(me)
        bm.from_mesh(me); bpy.data.meshes.remove(me)
    cutmesh = bpy.data.meshes.new(name + "-cutters"); bm.to_mesh(cutmesh); bm.free()
    cutobj = bpy.data.objects.new(name + "-cutters", cutmesh); bpy.context.collection.objects.link(cutobj)
    mod = panel.modifiers.new("perf", "BOOLEAN"); mod.operation = "DIFFERENCE"; mod.object = cutobj
    mod.solver = "EXACT"
    bpy.context.view_layer.objects.active = panel
    bpy.ops.object.modifier_apply(modifier="perf")
    bpy.data.objects.remove(cutobj, do_unlink=True)
    return panel, len(centers)


def count_open_holes(panel):
    """Genus of a slab with N through-holes: N = 1 - euler/2 where euler = V - E + F (closed manifold).
    Returns (measured_holes, euler, verts, edges, faces)."""
    me = panel.data
    V, E, F = len(me.vertices), len(me.edges), len(me.polygons)
    euler = V - E + F
    genus = int(round((2 - euler) / 2))   # closed orientable manifold: euler = 2 - 2*genus
    return genus, euler, V, E, F
