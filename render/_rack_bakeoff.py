# _rack_bakeoff.py · Problem 2 (depth into darkness) technique bake-off on the RM44 mesh.
# Measured lattice (MEASUREMENTS + rm44_measure): triangle period P=2.87mm, row pitch R=2.59mm,
# alternating orientation, ~0.5 open. A 200x120mm door tile at true scale, interior box +
# fan suggestion behind, judged under (1) raking strip ~12deg, (2) hero-ish front, (3) grazing.
#   A · REAL holes: boolean 2 triangular prisms on ONE cell -> array the perforated cell.
#   B · ALPHA mask: solidified plate, procedural triangle lattice driving transparency.
# Run: Blender -b -P render/_rack_bakeoff.py -- --tech A --light raking [--px 900]
import bpy, bmesh, math, sys, time

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
def arg(n, d=None):
    if n in argv:
        i = argv.index(n)
        if i + 1 < len(argv) and not argv[i+1].startswith("--"): return argv[i+1]
        return True
    return d

TECH = str(arg("--tech", "A")).upper()
LIGHT = str(arg("--light", "raking"))
PX = int(arg("--px", 900))
OUT = f"render/rack_previews/bake-{TECH}-{LIGHT}.png"

def mm(v): return v / 1000.0
P, R = 2.87, 2.59          # measured lattice (mm)
HOLE_SHRINK = 0.24         # web margin (mm) -> hole side ~ P-2*shrink, open ~0.5
TILE_W, TILE_H = 200.0, 120.0
THICK = 1.2                # door sheet thickness (mm)

# ---- scene -------------------------------------------------------------------------------
bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
sc.render.engine = "CYCLES"; sc.cycles.samples = 128
sc.cycles.use_denoising = True; sc.cycles.sample_clamp_indirect = 8.0
try:
    sc.cycles.denoiser = "OPENIMAGEDENOISE"
    sc.view_settings.view_transform = "AgX"; sc.view_settings.look = "AgX - High Contrast"
except Exception: pass
sc.view_settings.exposure = -0.70
sc.render.image_settings.file_format = "PNG"
sc.render.resolution_x = PX; sc.render.resolution_y = int(PX * 0.62)
w = bpy.data.worlds.new("void"); w.use_nodes = True
w.node_tree.nodes["Background"].inputs[0].default_value = (0.006, 0.006, 0.007, 1)
sc.world = w
try:
    prefs = bpy.context.preferences.addons["cycles"].preferences
    prefs.compute_device_type = "METAL"; prefs.get_devices_for_type("METAL")
    for d in prefs.devices: d.use = True
    sc.cycles.device = "GPU"
except Exception as e: print("gpu:", e)

def principled(name, base, rough, metallic=0.0):
    m = bpy.data.materials.new(name); m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*base, 1)
    b.inputs["Metallic"].default_value = metallic
    b.inputs["Roughness"].default_value = rough
    return m

def powder(name="pc"):
    m = principled(name, (0.028, 0.028, 0.030), 0.52)
    nt = m.node_tree; b = nt.nodes["Principled BSDF"]
    tc = nt.nodes.new("ShaderNodeTexCoord"); n = nt.nodes.new("ShaderNodeTexNoise")
    n.inputs["Scale"].default_value = 1400.0
    nt.links.new(tc.outputs["Object"], n.inputs["Vector"])
    bp = nt.nodes.new("ShaderNodeBump"); bp.inputs["Strength"].default_value = 0.06
    nt.links.new(n.outputs["Fac"], bp.inputs["Height"])
    nt.links.new(bp.outputs["Normal"], b.inputs["Normal"])
    return m

# ---- technique A · real holes ------------------------------------------------------------
def tri_prism(name, side, depth, up=True, loc=(0, 0, 0)):
    """Triangular prism cutter: equilateral triangle (side mm) in the X-Z plane,
    extruded along Y by depth mm, centered at origin."""
    me = bpy.data.meshes.new(name); bm = bmesh.new()
    hgt = side * math.sqrt(3) / 2.0
    s = 1.0 if up else -1.0
    pts = [(-side / 2.0, -s * hgt / 2.0), (side / 2.0, -s * hgt / 2.0), (0.0, s * hgt / 2.0)]
    vs = [bm.verts.new((mm(px), -mm(depth) / 2.0, mm(pz))) for (px, pz) in pts]
    f = bm.faces.new(vs)
    r = bmesh.ops.extrude_face_region(bm, geom=[f])
    for g in r["geom"]:
        if isinstance(g, bmesh.types.BMVert):
            g.co.y += mm(depth)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    bm.to_mesh(me); bm.free()
    ob = bpy.data.objects.new(name, me); bpy.context.collection.objects.link(ob)
    ob.location = loc
    bpy.context.view_layer.update()
    return ob

def build_A():
    # one cell = P wide x 2R tall, holding 1 up + 1 down triangle -> boolean once -> array
    cw, ch = mm(P), mm(2 * R)
    me = bpy.data.meshes.new("cell"); bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    for v in bm.verts:
        v.co.x *= cw; v.co.y *= mm(THICK); v.co.z *= ch
    bm.to_mesh(me); bm.free()
    cell = bpy.data.objects.new("cell", me); bpy.context.collection.objects.link(cell)
    side = P - 2 * HOLE_SHRINK
    cu = tri_prism("cu", side, THICK * 4, up=True, loc=(0, 0, mm(R / 2)))
    cd = tri_prism("cd", side, THICK * 4, up=False, loc=(0, 0, -mm(R / 2)))
    bpy.context.view_layer.objects.active = cell
    for c in (cu, cd):
        md = cell.modifiers.new("b", "BOOLEAN"); md.operation = "DIFFERENCE"
        md.solver = "EXACT"; md.object = c
        bpy.ops.object.modifier_apply(modifier=md.name)
        bpy.data.objects.remove(c, do_unlink=True)
    nx = int(TILE_W / P); nz = int(TILE_H / (2 * R))
    a1 = cell.modifiers.new("ax", "ARRAY"); a1.count = nx
    a1.use_relative_offset = False; a1.use_constant_offset = True
    a1.constant_offset_displace = (cw, 0, 0)
    a2 = cell.modifiers.new("az", "ARRAY"); a2.count = nz
    a2.use_relative_offset = False; a2.use_constant_offset = True
    a2.constant_offset_displace = (0, 0, ch)
    bpy.ops.object.modifier_apply(modifier="ax"); bpy.ops.object.modifier_apply(modifier="az")
    cell.location = (-mm(TILE_W)/2 + cw/2, 0, -mm(TILE_H)/2 + ch/2)
    cell.data.materials.append(powder("pcA"))
    for p in cell.data.polygons: p.use_smooth = False
    print(f"[A] cells {nx}x{nz}, tris {len(cell.data.polygons)}")
    return cell

# ---- technique B · alpha mask ------------------------------------------------------------
def build_B():
    bpy.ops.mesh.primitive_plane_add(size=1.0, rotation=(math.radians(90), 0, 0))
    pl = bpy.context.active_object; pl.name = "door"
    pl.scale = (mm(TILE_W), mm(TILE_H), 1.0)
    bpy.ops.object.transform_apply(scale=True)
    so = pl.modifiers.new("s", "SOLIDIFY"); so.thickness = mm(THICK)
    bpy.ops.object.modifier_apply(modifier="s")
    m = powder("pcB"); nt = m.node_tree
    b = nt.nodes["Principled BSDF"]; outn = nt.nodes["Material Output"]
    tc = nt.nodes.new("ShaderNodeTexCoord")
    sep = nt.nodes.new("ShaderNodeSeparateXYZ")
    nt.links.new(tc.outputs["Object"], sep.inputs["Vector"])
    def mk(op, a, bb=None, clamp=False):
        n = nt.nodes.new("ShaderNodeMath"); n.operation = op; n.use_clamp = clamp
        def put(i, v):
            if hasattr(v, "is_linked"): nt.links.new(v, n.inputs[i])
            else: n.inputs[i].default_value = v
        put(0, a)
        if bb is not None: put(1, bb)
        return n.outputs[0]
    u = mk("MULTIPLY", sep.outputs["X"], 1.0 / mm(P))
    v = mk("MULTIPLY", sep.outputs["Z"], 1.0 / mm(R))
    row = mk("FLOOR", v)
    par = mk("MODULO", row, 2.0)
    fu = mk("FRACT", mk("ADD", u, mk("MULTIPLY", par, 0.5)))
    fv = mk("FRACT", v)
    fvp = mk("ADD", mk("MULTIPLY", fv, mk("SUBTRACT", 1.0, mk("MULTIPLY", par, 2.0))), par)  # par? 1-fv : fv
    t = mk("MULTIPLY", mk("ABSOLUTE", mk("SUBTRACT", fu, 0.5)), 2.0)
    margin = HOLE_SHRINK / P
    hole = mk("GREATER_THAN", fvp, mk("ADD", t, margin * 2.0))
    tr = nt.nodes.new("ShaderNodeBsdfTransparent")
    mx = nt.nodes.new("ShaderNodeMixShader")
    nt.links.new(hole, mx.inputs["Fac"])
    nt.links.new(b.outputs["BSDF"], mx.inputs[1])
    nt.links.new(tr.outputs["BSDF"], mx.inputs[2])
    nt.links.new(mx.outputs["Shader"], outn.inputs["Surface"])
    m.blend_method = "HASHED" if hasattr(m, "blend_method") else None
    pl.data.materials.append(m)
    print("[B] alpha plate")
    return pl

# ---- shared interior ----------------------------------------------------------------------
def interior():
    dk = principled("int", (0.032, 0.032, 0.035), 0.80)
    me = bpy.data.meshes.new("ibox"); bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    for v in bm.verts:
        v.co.x *= mm(TILE_W); v.co.y *= mm(70); v.co.z *= mm(TILE_H)
    bmesh.ops.reverse_faces(bm, faces=bm.faces[:])   # normals inward
    bm.to_mesh(me); bm.free()
    ib = bpy.data.objects.new("ibox", me); bpy.context.collection.objects.link(ib)
    ib.location = (0, mm(36), 0)
    ib.data.materials.append(dk)
    # fan suggestion 30mm behind: dark disc + hub + 5 blades
    bpy.ops.mesh.primitive_cylinder_add(radius=mm(55), depth=mm(6), rotation=(math.radians(90), 0, 0), location=(0, mm(21), 0))
    fan = bpy.context.active_object
    fan.data.materials.append(principled("fan", (0.015, 0.015, 0.016), 0.6))
    for i in range(5):
        a = math.radians(72 * i + 15)
        bl = bpy.data.objects.new("bl", bpy.data.meshes.new("bl"))
        bmb = bmesh.new(); bmesh.ops.create_cube(bmb, size=1.0)
        for vv in bmb.verts:
            vv.co.x *= mm(38); vv.co.y *= mm(2); vv.co.z *= mm(16)
        bmb.to_mesh(bl.data); bmb.free()
        bpy.context.collection.objects.link(bl)
        bl.location = (mm(30) * math.cos(a), mm(16), mm(30) * math.sin(a))
        bl.rotation_euler = (math.radians(12), 0, a)
        bl.data.materials.append(principled("blm", (0.03, 0.03, 0.032), 0.5))
    # faint interior fill · must not touch exterior tone (tested by the gate later)
    ld = bpy.data.lights.new("ifill", "AREA"); ld.energy = 3.5; ld.size = mm(140)
    lo = bpy.data.objects.new("ifill", ld); bpy.context.collection.objects.link(lo)
    lo.location = (0, mm(20), mm(45)); lo.rotation_euler = (math.radians(-35), 0, 0)

# ---- lights + camera -----------------------------------------------------------------------
def light_cam():
    if LIGHT == "raking":
        ld = bpy.data.lights.new("strip", "AREA"); ld.energy = 2.4; ld.shape = "RECTANGLE"
        ld.size = mm(TILE_W * 1.2); ld.size_y = mm(5)
        lo = bpy.data.objects.new("strip", ld); bpy.context.collection.objects.link(lo)
        lo.location = (0, -mm(70), mm(TILE_H / 2 + 45))
        lo.rotation_euler = (math.radians(62), 0, 0)
        cam_yaw, cam_el, dist = 0.0, 2.0, 0.42
    elif LIGHT == "graze":
        ld = bpy.data.lights.new("key", "AREA"); ld.energy = 26; ld.size = 0.5
        lo = bpy.data.objects.new("key", ld); bpy.context.collection.objects.link(lo)
        lo.location = (-0.45, -0.5, 0.35); lo.rotation_euler = (math.radians(50), 0, math.radians(-40))
        cam_yaw, cam_el, dist = 55.0, 6.0, 0.40
    else:  # front hero-ish
        for nm, loc, e, sz in (("key", (-0.5, -0.55, 0.45), 30, 0.6),
                               ("fill", (0.15, -0.7, 0.05), 12, 0.8),
                               ("rim", (0.4, 0.5, 0.5), 16, 0.25)):
            ld = bpy.data.lights.new(nm, "AREA"); ld.energy = e; ld.size = sz
            lo = bpy.data.objects.new(nm, ld); bpy.context.collection.objects.link(lo)
            d = math.sqrt(sum(c * c for c in loc))
            lo.rotation_euler = (math.acos(-loc[2] / d) if d else 0, 0, math.atan2(-loc[0], loc[1]))
        cam_yaw, cam_el, dist = 12.0, 3.0, 0.5
    cd = bpy.data.cameras.new("c"); cd.lens = 85; cd.sensor_width = 36
    cam = bpy.data.objects.new("c", cd); bpy.context.collection.objects.link(cam)
    sc.camera = cam
    ya, el = math.radians(cam_yaw), math.radians(cam_el)
    cam.location = (dist * math.cos(el) * math.sin(ya), -dist * math.cos(el) * math.cos(ya), dist * math.sin(el))
    aim = bpy.data.objects.new("A", None); bpy.context.collection.objects.link(aim)
    c = cam.constraints.new("TRACK_TO"); c.target = aim
    c.track_axis = "TRACK_NEGATIVE_Z"; c.up_axis = "UP_Y"
    cd.dof.use_dof = True; cd.dof.focus_object = aim; cd.dof.aperture_fstop = 8.0

t0 = time.time()
(build_A if TECH == "A" else build_B)()
interior()
light_cam()
sc.render.filepath = OUT
bpy.ops.render.render(write_still=True)
print(f"bake-off {TECH}/{LIGHT} -> {OUT} in {time.time()-t0:.1f}s")
