# scratchpad · head-on rear-detail render of the Studio/Spark to audit the port row (the _audit_desktop
# 'front' framing is too low/edge-on to read the ports). Not a deliverable · verification only.
import bpy, math, os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); SCENE = os.path.join(HERE, "build_scene.py")
_argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
def _a(n, d=None):
    if n in _argv:
        i = _argv.index(n)
        return _argv[i + 1] if i + 1 < len(_argv) and not str(_argv[i + 1]).startswith("--") else True
    return d
WHICH = str(_a("--which", "studio")); NAME = str(_a("--name", "reardet"))
mm = lambda v: v / 1000.0
ns = {"__file__": SCENE, "__name__": "scene_defs"}
_sv = sys.argv; sys.argv = ["b", "--", "--only", "none", "--preview"]
exec(compile(open(SCENE).read(), SCENE, "exec"), ns); sys.argv = _sv
sc = ns["reset_scene"](); ns["enable_gpu"](sc)
sc.cycles.samples = 160
# VERIFICATION ONLY (scratchpad · not a deliverable · void-black law applies only to shipped frames):
# even world light so the aluminium rear face reads like a product photo and the port recesses show.
_w = bpy.data.worlds.get("World") or bpy.data.worlds.new("World"); sc.world = _w
_w.use_nodes = True
_bg = _w.node_tree.nodes.get("Background")
if _bg: _bg.inputs["Color"].default_value = (0.55, 0.56, 0.58, 1.0); _bg.inputs["Strength"].default_value = 1.1
if WHICH == "spark":
    ns["build_dgx_spark"](0.0, yaw_deg=180.0); pz = mm(20.0)
else:
    ns["build_mac_studio"](0.0, yaw_deg=180.0); pz = mm(23.0)
# GLOWPORTS · verification only: make the rear port-cavity material emissive so every cut cavity reads
# as a bright shape regardless of face lighting · a definitive check that all ports cut + placed right.
if "--glowports" in _argv:
    hit = 0
    for _m in bpy.data.materials:
        if (_m.name.startswith("mac-rear-port") or _m.name.startswith("mac-port")) and _m.use_nodes:
            _b = _m.node_tree.nodes.get("Principled BSDF")
            if not _b: continue
            for _en in ("Emission Color", "Emission"):
                if _en in _b.inputs:
                    _b.inputs[_en].default_value = (1.0, 0.4, 0.1, 1.0); hit += 1
            if "Emission Strength" in _b.inputs:
                _b.inputs["Emission Strength"].default_value = 30.0
    print("GLOWPORTS materials hit:", hit)
# rear face plane · at yaw 180 the original +D/2 rear rotates to -Y (toward the camera)
rear_face_y = -mm(197.0 / 2.0) if WHICH == "studio" else -mm(150.0 / 2.0)
# a SUN lamp pointing straight at the rear face (-Y) · even illumination, no falloff, lights the whole
# face so the dark port recesses read as holes in lit aluminium. Aim empty for the camera track.
sun = bpy.data.lights.new("sun", "SUN"); sun.energy = 3.2; sun.angle = math.radians(4.0)
sun_ob = bpy.data.objects.new("sun", sun); sun_ob.rotation_euler = (math.radians(78.0), 0, 0)  # light travels +Y (onto the camera-facing rear), slight down-rake
bpy.context.collection.objects.link(sun_ob)
sun2 = bpy.data.lights.new("sun2", "SUN"); sun2.energy = 1.4
sun2_ob = bpy.data.objects.new("sun2", sun2); sun2_ob.rotation_euler = (math.radians(96.0), 0, math.radians(18.0))  # +Y slight up-rake, from a side
bpy.context.collection.objects.link(sun2_ob)
aim = bpy.data.objects.new("aim", None); aim.location = (0.0, rear_face_y, mm(40.0)); bpy.context.collection.objects.link(aim)
# head-on camera, focus ON the rear face plane, deep DoF so the whole port row is sharp
cd = bpy.data.cameras.new("c"); cd.lens = 75.0; cd.sensor_width = 36.0
cam = bpy.data.objects.new("c", cd); bpy.context.collection.objects.link(cam); sc.camera = cam
cam.location = (0.0, -0.46, mm(40.0))
con = cam.constraints.new("TRACK_TO"); con.target = aim; con.track_axis = "TRACK_NEGATIVE_Z"; con.up_axis = "UP_Y"
cd.dof.use_dof = True; cd.dof.focus_object = aim; cd.dof.aperture_fstop = 22.0
sc.render.resolution_x, sc.render.resolution_y = (1800, 900)
sc.render.resolution_percentage = 100
ns["render_to"](f"render/previews/reardet-{WHICH}-{NAME}.png")
print("REARDET done", WHICH, NAME)
