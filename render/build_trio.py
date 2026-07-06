# render/build_trio.py · THE SCALE TRIO · the 6-GPU RTX 5090 rack (base) with the Mac Studio +
# DGX Spark on top, at TRUE metric scale, one lit scene. The site's frame-of-reference hero.
#
# Composes the two frozen builders WITHOUT editing them: build_scene.py is exec'd with `--only
# none` (its dispatch no-ops) and build_rack.py with `--part defs` (a no-op branch), each into its
# own namespace so their helper names never collide. Then we build the rack, snapshot-diff the
# Studio + Spark builders to grab their objects, and seat them on the rack cap. Dash gate: middot.
#
#   /Applications/Blender.app/Contents/MacOS/Blender -b -P render/build_trio.py -- --shot q34
import bpy, sys, os, math

HERE = os.path.dirname(os.path.abspath(__file__))
RACK_PATH = os.path.join(HERE, "build_rack.py")
SCENE_PATH = os.path.normpath(os.path.join(HERE, "..", "..", "model-refinement", "render", "build_scene.py"))

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
def _arg(name, d=None):
    if name in argv:
        i = argv.index(name)
        return argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("--") else True
    return d
SHOT = str(_arg("--shot", "q34"))
PREVIEW = bool(_arg("--preview", False))
POST = bool(_arg("--post", False))
HIRES = bool(_arg("--hires", False))   # definitive hero: bigger res + more samples

def load(path, argv_after, name):
    """exec a builder file into its own namespace with a no-op dispatch · returns the namespace."""
    src = open(path).read()
    ns = {"__file__": path, "__name__": name, "__builtins__": __builtins__}
    saved = sys.argv
    sys.argv = ["blender", "--"] + argv_after
    try:
        exec(compile(src, path, "exec"), ns)
    finally:
        sys.argv = saved
    return ns

# load both builders (neither builds a scene or renders under these args)
SCN = load(SCENE_PATH, ["--only", "none"] + (["--preview"] if PREVIEW else []), "scene_defs")
RK = load(RACK_PATH, ["--part", "defs"] + (["--preview"] if PREVIEW else []) + (["--post"] if POST else [])
          + (["--samples", "820"] if HIRES else []), "rack_defs")

# --- build the shared scene via the rack's setup, then the rack itself ---
sc = RK["reset_scene"]()
RK["enable_gpu"](sc)
RK["build_frame"]()
RK["build_gpu_row"]()
RACK_TOP = RK["RACK"]["H"] / 1000.0    # 0.700 m (RACK dims are mm)

def snapshot(): return set(bpy.data.objects)
def min_z(objs):
    z = 1e9
    for o in objs:
        if o.type != "MESH" or not o.data.vertices: continue
        for v in o.data.vertices:
            wz = (o.matrix_world @ v.co).z
            if wz < z: z = wz
    return z
def seat(objs, dx, dy):
    """drop the object group onto the rack cap (base -> RACK_TOP) and nudge in x/y."""
    dz = RACK_TOP - min_z(objs) + 0.001
    for o in objs:
        o.location.x += dx; o.location.y += dy; o.location.z += dz

# Studio (197) left-of-centre, Spark (150) right-of-centre, both facing front (-Y), on the cap.
b0 = snapshot(); SCN["build_mac_studio"](loc_x=0.0, yaw_deg=0.0); studio = snapshot() - b0
b0 = snapshot(); SCN["build_dgx_spark"](loc_x=0.0, yaw_deg=0.0); spark = snapshot() - b0
seat(studio, -0.120, -0.020)
seat(spark, 0.135, -0.020)

# --- trio rig (frames the ~0.96w x ~0.80h assembly · dark-object hero, void black) ---
aim = bpy.data.objects.new("Aim", None); aim.location = (0, 0, 0.42)
bpy.context.collection.objects.link(aim)
def area(name, loc, size, energy, color=(1, 1, 1), sx=None):
    ld = bpy.data.lights.new(name, "AREA"); ld.energy = energy; ld.color = color
    if sx is not None: ld.shape = "RECTANGLE"; ld.size = sx; ld.size_y = size
    else: ld.size = size
    ob = bpy.data.objects.new(name, ld); ob.location = loc; bpy.context.collection.objects.link(ob)
    c = ob.constraints.new("TRACK_TO"); c.target = aim; c.track_axis = "TRACK_NEGATIVE_Z"; c.up_axis = "UP_Y"
area("key", (-1.2, -1.4, 1.5), 1.0, 135, (1.0, 0.99, 0.97))
area("rim", (1.0, 1.3, 1.35), 0.08, 95, (0.93, 0.96, 1.0), sx=1.2)
area("fill", (0.2, -1.7, 0.7), 1.5, 34, (0.97, 0.98, 1.0))
bpy.ops.mesh.primitive_plane_add(size=12.0, location=(0, 0, 0))
fl = bpy.context.active_object; fl.name = "floor"
fl.data.materials.append(RK["floor_mat"]())  # smudge-varied reflection · grounds without a perfect-mirror CG tell (panel-4 #4)

cd = bpy.data.cameras.new("cam"); cd.lens = 66.0; cd.sensor_width = 36.0
cam = bpy.data.objects.new("cam", cd); bpy.context.collection.objects.link(cam); sc.camera = cam
dist = 3.05
yaw, elev = (0.0, 3.0) if SHOT in ("front",) else (30.0, 8.0)
ya, el = math.radians(yaw), math.radians(elev)
ax, ay, az = aim.location
cam.location = (ax + dist * math.cos(el) * math.sin(ya), ay - dist * math.cos(el) * math.cos(ya), az + dist * math.sin(el))
c = cam.constraints.new("TRACK_TO"); c.target = aim; c.track_axis = "TRACK_NEGATIVE_Z"; c.up_axis = "UP_Y"
cd.dof.use_dof = True; cd.dof.focus_object = aim; cd.dof.aperture_fstop = 14.0
sc.render.resolution_x, sc.render.resolution_y = (2880, 2304) if HIRES else (2000, 1600)

out = "render/rack_previews/trio-" + SHOT + ".png"
RK["render_to"](out)
print("TRIO done:", out)
