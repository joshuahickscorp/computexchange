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
    # 2) locate + REMOVE the pass-2 shader vent panel ("mac-vent-mesh") — it was a material illusion
    vent = bpy.data.objects.get("mac-vent-mesh")
    vent_center = None
    removed = False
    if vent is not None:
        from mathutils import Vector
        mn = Vector((1e9,)*3); mx = Vector((-1e9,)*3)
        for c in vent.bound_box:
            w = vent.matrix_world @ Vector(c)
            for i in range(3): mn[i]=min(mn[i],w[i]); mx[i]=max(mx[i],w[i])
        vent_center = ((mn.x+mx.x)/2, (mn.y+mx.y)/2, (mn.z+mx.z)/2)
        bpy.data.objects.remove(vent, do_unlink=True); removed = True
    # 3) build the REAL perforated panel at the vent location (rear face at +Y for yaw=0)
    perf = _load_module(perf_path, "perf")
    # default vent center in model space: x=0, rear face y≈+D/2, z≈0.056 (from MCP measure)
    cx, cy, cz = (0.0, 0.0999, 0.056) if vent_center is None else (0.0, vent_center[1], vent_center[2])
    panel, centers = perf.build_perforated_panel(field_w_mm=173.0, field_h_mm=50.0, thickness_mm=3.0,
                                                 pitch_mm=pitch_mm, hole_d_mm=hole_d_mm,
                                                 center=(cx, cy, cz), name="mac-rear-perf-v2")
    through, blind = perf.count_through_holes_raycast(panel, centers, thickness_mm=3.0)
    # material: brushed aluminium (matches shell), so the panel reads as integrated, not a black plate
    mat = bpy.data.materials.new("mac-rear-perf-alu"); mat.use_nodes = True
    pb = mat.node_tree.nodes["Principled BSDF"]; pb.inputs[0].default_value = (0.62,0.62,0.64,1)
    try: pb.inputs["Roughness"].default_value = 0.42; pb.inputs["Metallic"].default_value = 0.85
    except Exception: pass
    panel.data.materials.append(mat)
    return {"baseline_vent_removed": removed, "perf_intended": len(centers),
            "perf_through_holes": through, "perf_blind": blind, "panel_name": panel.name,
            "vent_center_m": [round(v,4) for v in (cx,cy,cz)]}
