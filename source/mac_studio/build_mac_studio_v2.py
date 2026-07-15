#!/usr/bin/env python3
"""Mac Studio v2 builder (Pass-6).

Authoritative v2 = the accepted baseline body/ports built by the existing procedural builder, with
the pass-2 SHADER-only rear exhaust replaced by a REAL physically-perforated panel (474+ boolean
through-holes). Additional accepted candidate groups (semantic port cutters, AC/headphone/power,
recesses/seams) are layered on top in later iterations. No absolute paths baked into deliverables.

build_mac_studio_v2(builder_path, yaw_deg) -> dict(baseline_vent_removed, perf_holes, panel_name)
"""
import bpy, sys, importlib.util, os

def _load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def build_mac_studio_v2(builder_path, perf_path, yaw_deg=0.0,
                        pitch_mm=4.2, hole_d_mm=2.8):
    # 1) build the accepted baseline (body + ports + shader vent) via the existing builder
    argv_bak = list(sys.argv)
    sys.argv = ["b", "--", "--only", "none"]
    g = {"__name__": "macbase", "__file__": builder_path, "__builtins__": __builtins__}
    exec(compile(open(builder_path).read(), builder_path, "exec"), g)
    for o in list(bpy.data.objects): bpy.data.objects.remove(o, do_unlink=True)
    g["reset_scene"]()
    for o in list(bpy.data.objects): bpy.data.objects.remove(o, do_unlink=True)
    g["build_mac_studio"](0.0, yaw_deg=yaw_deg)
    sys.argv = argv_bak
    # 2) CUT real hex through-holes directly INTO the baseline vent panel (it is already correctly
    #    positioned + visible in the baseline; no remove/replace -> no dark-void recess bug).
    from mathutils import Vector
    perf = _load_module(perf_path, "perf")
    vent = bpy.data.objects.get("mac-vent-mesh")
    if vent is None:
        return {"error": "mac-vent-mesh not found in baseline"}
    mn = Vector((1e9,)*3); mx = Vector((-1e9,)*3)
    for c in vent.bound_box:
        w = vent.matrix_world @ Vector(c)
        for i in range(3): mn[i]=min(mn[i],w[i]); mx[i]=max(mx[i],w[i])
    vw = (mx.x-mn.x)*1000.0; vh = (mx.z-mn.z)*1000.0
    vx = (mn.x+mx.x)/2.0; vy = (mn.y+mx.y)/2.0; vz = (mn.z+mx.z)/2.0
    n_holes = perf.perforate_object(vent, field_w_mm=vw*0.96, field_h_mm=vh*0.9, thickness_mm=1.0,
                                    pitch_mm=pitch_mm, hole_d_mm=hole_d_mm, center=(vx, vy, vz))
    # aluminium so the perforated grille reads as metal (replaces the shader illusion material)
    mat = bpy.data.materials.new("mac-rear-perf-alu"); mat.use_nodes = True
    pb = mat.node_tree.nodes["Principled BSDF"]; pb.inputs[0].default_value = (0.62,0.62,0.64,1)
    try: pb.inputs["Roughness"].default_value = 0.42; pb.inputs["Metallic"].default_value = 0.85
    except Exception: pass
    vent.data.materials.clear(); vent.data.materials.append(mat)
    return {"baseline_vent_perforated": True, "perf_through_holes": n_holes,
            "vent_extent_mm": [round(vw,1), round(vh,1)], "panel_name": vent.name,
            "vent_center_m": [round(vx,4), round(vy,4), round(vz,4)]}
