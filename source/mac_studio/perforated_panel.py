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
    # build ONE joined cutter mesh: accumulate all cylinders DIRECTLY in one bmesh (no per-cutter
    # to_mesh round-trips, which previously dropped most cutters). Cone axis Z -> rotate to Y.
    from mathutils import Matrix
    bm = bmesh.new()
    rotX = Matrix.Rotation(math.radians(90), 3, 'X')
    for (hx, hz) in centers:
        ret = bmesh.ops.create_cone(bm, cap_ends=True, segments=seg, radius1=r, radius2=r, depth=T * 2.0)
        vnew = ret["verts"]
        bmesh.ops.rotate(bm, verts=vnew, matrix=rotX)                       # Z-axis cylinder -> Y-axis
        bmesh.ops.translate(bm, verts=vnew, vec=Vector((hx, cy, hz)))       # to hole center
    cutmesh = bpy.data.meshes.new(name + "-cutters"); bm.to_mesh(cutmesh); bm.free()
    cutobj = bpy.data.objects.new(name + "-cutters", cutmesh); bpy.context.collection.objects.link(cutobj)
    mod = panel.modifiers.new("perf", "BOOLEAN"); mod.operation = "DIFFERENCE"; mod.object = cutobj
    mod.solver = "EXACT"
    bpy.context.view_layer.objects.active = panel
    bpy.ops.object.modifier_apply(modifier="perf")
    bpy.data.objects.remove(cutobj, do_unlink=True)
    # recompute normals (boolean output can leave inverted faces -> renders black); make consistent + outward
    bm2 = bmesh.new(); bm2.from_mesh(panel.data)
    bmesh.ops.recalc_face_normals(bm2, faces=bm2.faces)
    bm2.to_mesh(panel.data); bm2.free(); panel.data.update()
    return panel, centers


def _hex_centers(cx, cz, W, Hh, p, margin):
    centers = []; row = 0; z = -Hh/2.0 + margin
    dz = p*0.866
    while z <= Hh/2.0 - margin:
        xoff = (p/2.0) if (row % 2) else 0.0
        x = -W/2.0 + margin + xoff
        while x <= W/2.0 - margin:
            centers.append((cx + x, cz + z)); x += p
        z += dz; row += 1
    return centers


def perforate_object(target, field_w_mm, field_h_mm, thickness_mm, pitch_mm, hole_d_mm,
                     center, seg=12):
    """Boolean-cut a hex through-hole field DIRECTLY into an existing mesh object `target`
    (e.g. the baseline vent panel, already correctly positioned). Returns the hole count.
    Cutter cylinders run along Y (through the rear face). Recomputes normals after."""
    from mathutils import Matrix
    W, Hh, T = _mm(field_w_mm), _mm(field_h_mm), _mm(thickness_mm)
    p, r = _mm(pitch_mm), _mm(hole_d_mm)/2.0
    cx, cy, cz = center
    centers = _hex_centers(cx, cz, W, Hh, p, p)
    bm = bmesh.new(); rotX = Matrix.Rotation(math.radians(90), 3, 'X')
    for (hx, hz) in centers:
        ret = bmesh.ops.create_cone(bm, cap_ends=True, segments=seg, radius1=r, radius2=r, depth=T*3.0)
        vnew = ret["verts"]
        bmesh.ops.rotate(bm, verts=vnew, matrix=rotX)
        bmesh.ops.translate(bm, verts=vnew, vec=Vector((hx, cy, hz)))
    cutmesh = bpy.data.meshes.new("cut"); bm.to_mesh(cutmesh); bm.free()
    cutobj = bpy.data.objects.new("cut", cutmesh); bpy.context.collection.objects.link(cutobj)
    mod = target.modifiers.new("perf", "BOOLEAN"); mod.operation = "DIFFERENCE"
    mod.object = cutobj; mod.solver = "EXACT"
    bpy.context.view_layer.objects.active = target
    bpy.ops.object.modifier_apply(modifier="perf")
    bpy.data.objects.remove(cutobj, do_unlink=True)
    # NOTE: do NOT recalc_face_normals here — the boolean preserves the target's original (correct,
    # camera-facing) face normals; a global recalc can flip the visible face to a backface (renders dark).
    target.data.update()
    return len(centers)


def count_through_holes_raycast(panel, centers, thickness_mm=3.0):
    """Ground-truth through-hole count: cast a ray along +Y at each hole center through the panel.
    A clean THROUGH-hole => the axial ray hits nothing (passes through empty). A blind pit or solid
    spot => the ray hits. Returns (through_count, blind_or_solid_count)."""
    import bpy
    from mathutils import Vector
    depsgraph = bpy.context.evaluated_depsgraph_get()
    mw = panel.matrix_world; mwi = mw.inverted()
    t = thickness_mm / 1000.0
    through = 0; hit = 0
    for (hx, hz) in centers:
        origin = mwi @ Vector((hx, -(t + 0.001), hz))          # just behind the panel, local space
        direction = (mwi.to_3x3() @ Vector((0.0, 1.0, 0.0))).normalized()
        res, loc, nrm, idx = panel.ray_cast(origin, direction, distance=2 * t + 0.002)
        if res: hit += 1
        else: through += 1
    return through, hit


def count_open_holes(panel):
    """Genus of a slab with N through-holes: N = 1 - euler/2 where euler = V - E + F (closed manifold).
    Returns (measured_holes, euler, verts, edges, faces)."""
    me = panel.data
    V, E, F = len(me.vertices), len(me.edges), len(me.polygons)
    euler = V - E + F
    genus = int(round((2 - euler) / 2))   # closed orientable manifold: euler = 2 - 2*genus
    return genus, euler, V, E, F
