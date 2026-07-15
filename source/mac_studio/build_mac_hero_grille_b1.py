#!/usr/bin/env python3
"""B1: Mac Studio UPPER HERO REAR EXHAUST as real physical open geometry, built to the
MEASURED reference spec (annotations/mac_studio/rear_grille_reference.json).

Reference (measured from apple_back.jpg @ mm/px 0.08058):
  field 171.5 x 48.5 mm, normalized x[0.064,0.936] y[0.050,0.554] (image-y from body top)
  -> in model Z (from body base, body 95mm): z 42.4 .. 90.25 mm, center z 66.3
  pitch 1.93 mm, hole diameter 0.92 mm, open-area 0.428

The baseline vent (mac-vent-mesh) sits at z 31..81 (center 56) -> ~10mm TOO LOW and 2x too coarse.
B1 repositions/resizes it to the measured bounds and perforates at the measured pitch/diameter.

build_hero_grille(builder_path, perf_path, spec_path, yaw_deg) -> dict metrics
"""
import bpy, sys, json, importlib.util
from mathutils import Vector


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def build_hero_grille(builder_path, perf_path, spec_path, yaw_deg=0.0, body_h_mm=95.0):
    spec = json.load(open(spec_path))
    nb = spec["grille_norm_bounds"]
    # convert normalized image-y (from body TOP) -> model Z (from body BASE)
    z_top_mm = body_h_mm * (1.0 - nb["y0"])
    z_bot_mm = body_h_mm * (1.0 - nb["y1"])
    field_h_mm = z_top_mm - z_bot_mm
    field_z_mm = (z_top_mm + z_bot_mm) / 2.0
    field_w_mm = spec["grille_size_mm"][0]
    pitch_mm = spec["pitch_mm"]["x"]
    hole_d_mm = spec["hole_diameter_mm"]

    argv_bak = list(sys.argv); sys.argv = ["b", "--", "--only", "none"]
    g = {"__name__": "macb1", "__file__": builder_path, "__builtins__": __builtins__}
    exec(compile(open(builder_path).read(), builder_path, "exec"), g)
    for o in list(bpy.data.objects): bpy.data.objects.remove(o, do_unlink=True)
    g["reset_scene"]()
    for o in list(bpy.data.objects): bpy.data.objects.remove(o, do_unlink=True)
    g["build_mac_studio"](0.0, yaw_deg=yaw_deg)
    sys.argv = argv_bak

    vent = bpy.data.objects.get("mac-vent-mesh")
    if vent is None:
        return {"error": "mac-vent-mesh not found"}
    # current vent extent
    mn = Vector((1e9,)*3); mx = Vector((-1e9,)*3)
    for c in vent.bound_box:
        w = vent.matrix_world @ Vector(c)
        for i in range(3): mn[i] = min(mn[i], w[i]); mx[i] = max(mx[i], w[i])
    cur_w = (mx.x-mn.x)*1000.0; cur_h = (mx.z-mn.z)*1000.0
    cur_z = (mn.z+mx.z)/2.0*1000.0; vy = (mn.y+mx.y)/2.0
    # REPOSITION + RESIZE the vent to the measured reference bounds
    sx = field_w_mm/cur_w if cur_w > 0 else 1.0
    sz = field_h_mm/cur_h if cur_h > 0 else 1.0
    vent.scale = (vent.scale.x*sx, vent.scale.y, vent.scale.z*sz)
    bpy.context.view_layer.update()
    vent.location.z += (field_z_mm - cur_z)/1000.0
    bpy.context.view_layer.update()
    bpy.ops.object.select_all(action='DESELECT'); vent.select_set(True)
    bpy.context.view_layer.objects.active = vent
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    bpy.context.view_layer.update()
    # re-measure after move
    mn = Vector((1e9,)*3); mx = Vector((-1e9,)*3)
    for c in vent.bound_box:
        w = vent.matrix_world @ Vector(c)
        for i in range(3): mn[i] = min(mn[i], w[i]); mx[i] = max(mx[i], w[i])
    vx = (mn.x+mx.x)/2.0; vy = (mn.y+mx.y)/2.0; vz = (mn.z+mx.z)/2.0

    # PERFORATE at the MEASURED pitch/diameter (real boolean through-holes).
    # CRITICAL: the cutter length must EXCEED the panel's actual Y thickness or the holes are BLIND
    # (root cause of the previous "grille renders solid white" failures: 1mm cutters vs a ~3mm panel).
    panel_thick_mm = (mx.y - mn.y) * 1000.0
    cutter_len_mm = max(8.0, panel_thick_mm * 4.0)
    perf = _load(perf_path, "perf")
    n_holes = perf.perforate_object(vent, field_w_mm=field_w_mm*0.985, field_h_mm=field_h_mm*0.97,
                                    thickness_mm=cutter_len_mm, pitch_mm=pitch_mm, hole_d_mm=hole_d_mm,
                                    center=(vx, vy, vz))
    # CRITICAL: the vent straddles the body surface (vent y 97.15..100.15 vs body face 98.5), so holes
    # punched through the thin vent immediately hit the SOLID BODY behind -> white shows through every
    # hole and the grille reads invisible. Cut a real RECESS into the body behind the field so the holes
    # open into a dark interior (this is what makes a perforation actually read as a grille).
    body = bpy.data.objects.get("mac-studio")
    if body is not None:
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=(vx, vy - (0.010 if vy > 0 else -0.010), vz))
        rec = bpy.context.active_object; rec.name = "hero-grille-recess-cutter"
        rec.scale = ((field_w_mm/1000.0)*0.5*0.99, 0.010, (field_h_mm/1000.0)*0.5*0.99)
        bpy.ops.object.transform_apply(scale=True)
        m = body.modifiers.new("heroRecess", "BOOLEAN"); m.operation = "DIFFERENCE"; m.object = rec
        bpy.context.view_layer.objects.active = body
        bpy.ops.object.modifier_apply(modifier=m.name)
        bpy.data.objects.remove(rec, do_unlink=True)
        # dark interior so the holes read as real openings
        dk = bpy.data.materials.new("mac-grille-interior-dark"); dk.use_nodes = True
        dp = dk.node_tree.nodes["Principled BSDF"]; dp.inputs[0].default_value = (0.02, 0.02, 0.025, 1)
        try: dp.inputs["Roughness"].default_value = 0.95
        except Exception: pass
        body.data.materials.append(dk)
    mat = bpy.data.materials.new("mac-hero-grille-alu"); mat.use_nodes = True
    pb = mat.node_tree.nodes["Principled BSDF"]; pb.inputs[0].default_value = (0.62,0.62,0.64,1)
    try: pb.inputs["Roughness"].default_value = 0.42; pb.inputs["Metallic"].default_value = 0.85
    except Exception: pass
    vent.data.materials.clear(); vent.data.materials.append(mat)
    return {"target_field_mm": [round(field_w_mm,2), round(field_h_mm,2)],
            "target_z_center_mm": round(field_z_mm,2),
            "baseline_z_center_mm": round(cur_z,2),
            "z_shift_mm": round(field_z_mm-cur_z,2),
            "pitch_mm": pitch_mm, "hole_d_mm": hole_d_mm,
            "holes_cut": n_holes, "panel": vent.name,
            "target_open_area": spec["open_area_fraction"]}
