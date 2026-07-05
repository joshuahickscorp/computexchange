# build_rack.py · the THIRD oracle: a person-owned 42U homelab GPU rack.
# Separate builder from build_scene.py (the CLOSED desktop masters) · helpers are COPIED in,
# not imported, so the frozen file is never touched. Archetype: D-ARCH A-prosumer (see
# render/ref/rack/D-ARCH.md). Every dimension traces to render/MEASUREMENTS.md (RACK section),
# anchored on the EIA-310 U-module (1U = 44.45 mm exactly). Dash gate: middot only.
#
# Run headless from the repo root:
#   /Applications/Blender.app/Contents/MacOS/Blender -b -P render/build_rack.py -- \
#       --shot q34 --preview
#   ... -- --shot frame-front   (empty-frame proof + dark-rig probe)

import bpy, bmesh, math, sys, time

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
def arg(name, default=None):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            return argv[i + 1]
        return True
    return default

PREVIEW = bool(arg("--preview", False))
SHOT = str(arg("--shot", "q34"))
SAMPLES = int(arg("--samples", 96 if PREVIEW else 512))
OUT = str(arg("--out", "render/rack_previews/"))
if not OUT.endswith("/"): OUT += "/"

# ---- scale · 1 Blender unit = 1 metre (metric, true scale for the eventual scale trio) ----
S = 1.0
def mm(v): return v / 1000.0

U = 44.45  # THE anchor · mm

# measured constants (MEASUREMENTS.md · RACK section) --------------------------------------
# R0.1 (geo-audit S5): outer width was 750 · the rail-derived pixel scale on frame-frame-front
# proved the render at ~760mm vs the real NetShelter SX 600mm. Rails are anchored on HOLE_SPAN
# (unchanged), so W drives only posts/walls/cap/plinth · at W=600 the rail-to-outer-edge lands
# 67.4mm, matching the AR3140 ref's 67.5mm. AUTOPSY: 750 was a wide-variant assumption; the
# archetype (D-ARCH A · NetShelter SX 42U) is the 600mm standard cabinet.
RACK = dict(W=600.0, H=1991.0, D=1070.0, Ucount=42)
PANEL_W = 482.6           # 19in ear-to-ear
HOLE_SPAN = 465.12        # rail hole-center span
SQ_HOLE = 9.5             # square cage-nut hole
HOLE_OFF = (6.35, 22.25, 38.10)   # hole centers from each U boundary
PLINTH = 100.0            # base below U1 (1991 - 42*44.45 = 124.1 total; plinth + cap)
RAIL_DATUM = PLINTH       # z of the bottom of U1

def u_z(n):
    """z (mm) of the bottom face of rack-unit n (n=1 is the lowest)."""
    return RAIL_DATUM + (n - 1) * U

# ---- scene / device ----------------------------------------------------------------------
def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.samples = SAMPLES
    sc.cycles.use_adaptive_sampling = True
    sc.cycles.adaptive_threshold = 0.005
    sc.cycles.use_denoising = True
    sc.cycles.sample_clamp_indirect = 8.0   # kill cavity fireflies (L13 desktop lesson · a rack is all cavities)
    try:
        sc.cycles.denoiser = "OPENIMAGEDENOISE"
        sc.cycles.denoising_input_passes = "RGB_ALBEDO_NORMAL"
    except Exception as e:
        print("denoiser fallback:", e)
    try:
        sc.view_settings.view_transform = "AgX"
        sc.view_settings.look = "AgX - High Contrast"
    except Exception:
        sc.view_settings.view_transform = "Filmic"
    sc.view_settings.exposure = -0.70
    sc.render.film_transparent = False
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGBA"
    sc.render.resolution_percentage = 40 if PREVIEW else 100
    w = bpy.data.worlds.new("void"); w.use_nodes = True
    w.node_tree.nodes["Background"].inputs[0].default_value = (0.006, 0.006, 0.007, 1)  # void black (site --bg)
    sc.world = w
    return sc

def enable_gpu(sc):
    chosen = "CPU"
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        for dt in ("METAL", "OPTIX", "CUDA"):
            try: prefs.compute_device_type = dt
            except TypeError: continue
            prefs.get_devices_for_type(dt)
            got = False
            for d in prefs.devices:
                if d.type == dt: d.use = True; got = True
            if got: sc.cycles.device = "GPU"; chosen = dt; break
    except Exception as e:
        print("GPU setup failed, CPU:", e)
    print("render device:", chosen)
    return chosen

# ---- geometry helpers (copied from build_scene.py · frozen file untouched) ----------------
def box(name, w, d, h, loc=(0, 0, 0)):
    me = bpy.data.meshes.new(name); bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    for v in bm.verts:
        v.co.x *= w; v.co.y *= d; v.co.z *= h
    bm.to_mesh(me); bm.free()
    ob = bpy.data.objects.new(name, me); bpy.context.collection.objects.link(ob)
    ob.location = loc
    return ob

def rounded_box(name, w, d, h, r_corner, seg=8):
    me = bpy.data.meshes.new(name); bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    for v in bm.verts:
        v.co.x *= w; v.co.y *= d; v.co.z *= h
    eps = min(w, d, h) * 1e-4
    vert = [e for e in bm.edges
            if abs(e.verts[0].co.x - e.verts[1].co.x) < eps and abs(e.verts[0].co.y - e.verts[1].co.y) < eps]
    if r_corner > 0:
        bmesh.ops.bevel(bm, geom=vert, offset=r_corner, segments=seg, profile=0.5, affect="EDGES")
    bm.to_mesh(me); bm.free()
    ob = bpy.data.objects.new(name, me); bpy.context.collection.objects.link(ob)
    return ob

def smooth(ob, deg=30.0):
    bpy.context.view_layer.objects.active = ob; ob.select_set(True)
    bpy.ops.object.shade_auto_smooth(angle=math.radians(deg)); ob.select_set(False)

def apply_mod(ob):
    bpy.context.view_layer.objects.active = ob
    for m in list(ob.modifiers):
        bpy.ops.object.modifier_apply(modifier=m.name)

# ---- materials ---------------------------------------------------------------------------
def principled(name, base, rough, metallic=0.0, coat=0.0):
    m = bpy.data.materials.new(name); m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*base, 1)
    b.inputs["Metallic"].default_value = metallic
    b.inputs["Roughness"].default_value = rough
    try: b.inputs["Coat Weight"].default_value = coat
    except KeyError: pass
    return m

def add_bevel(m, radius=0.20, samples=4):
    nt = m.node_tree; b = nt.nodes["Principled BSDF"]
    bev = nt.nodes.new("ShaderNodeBevel"); bev.samples = samples
    bev.inputs["Radius"].default_value = mm(radius)
    ni = b.inputs["Normal"]
    if ni.is_linked: nt.links.new(ni.links[0].from_socket, bev.inputs["Normal"])
    nt.links.new(bev.outputs["Normal"], ni)
    return m

def powder_coat(name="powder-black", base=(0.028, 0.028, 0.030), rough=0.52):
    # matte satin black powder-coat · NEW material target (not bead-blast, not anodize).
    # Reference reads L~16 in bright studio light (rm44_front_A). A fine bump gives the
    # orange-peel texture that catches the key as a soft sheen · the sheen is what carves a
    # black object out of void-black (dark-object doctrine, RACK-BUILD-PLAN section 1).
    m = principled(name, base, rough, metallic=0.0)
    nt = m.node_tree; b = nt.nodes["Principled BSDF"]
    tc = nt.nodes.new("ShaderNodeTexCoord")
    n = nt.nodes.new("ShaderNodeTexNoise")
    n.inputs["Scale"].default_value = 1400.0 / S; n.inputs["Detail"].default_value = 2.0
    nt.links.new(tc.outputs["Object"], n.inputs["Vector"])
    mapr = nt.nodes.new("ShaderNodeMapRange")
    mapr.inputs["To Min"].default_value = rough - 0.04; mapr.inputs["To Max"].default_value = rough + 0.04
    nt.links.new(n.outputs["Fac"], mapr.inputs["Value"])
    nt.links.new(mapr.outputs["Result"], b.inputs["Roughness"])
    bump = nt.nodes.new("ShaderNodeBump"); bump.inputs["Strength"].default_value = 0.06
    nt.links.new(n.outputs["Fac"], bump.inputs["Height"])
    nt.links.new(bump.outputs["Normal"], b.inputs["Normal"])
    return add_bevel(m)

def interior_dark(name="interior"):
    # cavity interior · albedo NEVER 0 so wall gradients read (desktop wave-2 lesson).
    return principled(name, (0.020, 0.020, 0.022), 0.80, metallic=0.0)

# ---- the enclosure frame -----------------------------------------------------------------
def rail_with_holes(x, name):
    """One front EIA rail: a 1U flange segment with 3 square holes, arrayed x42.
    Placed at world x, front face toward -Y. The square-hole pattern is THE recognizable
    rack signature · hole size + per-U offsets are exact (MEASUREMENTS)."""
    flange_w, thick = mm(30.0), mm(2.0)
    seg = box(name + "-seg", flange_w, thick, mm(U), (x, mm(-490.0), u_z(1) / 1000.0 + mm(U) / 2.0))
    # cut 3 square holes through the Y thickness at the measured offsets
    cutters = []
    for off in HOLE_OFF:
        cz = u_z(1) / 1000.0 + mm(off)
        c = box("hcut", mm(SQ_HOLE), thick * 4, mm(SQ_HOLE), (x, mm(-490.0), cz))
        cutters.append(c)
    bpy.context.view_layer.objects.active = seg
    for c in cutters:
        md = seg.modifiers.new("cut", "BOOLEAN"); md.operation = "DIFFERENCE"
        md.solver = "EXACT"; md.object = c
        bpy.ops.object.modifier_apply(modifier=md.name)
        bpy.data.objects.remove(c, do_unlink=True)
    arr = seg.modifiers.new("arr", "ARRAY"); arr.count = RACK["Ucount"]
    arr.use_relative_offset = False; arr.use_constant_offset = True
    arr.constant_offset_displace = (0, 0, mm(U))
    bpy.ops.object.modifier_apply(modifier=arr.name)
    return seg

def build_frame():
    """Open 42U enclosure skeleton (door removed so the fill will read). Posts, top cap,
    base plinth, two 0U side channels, four EIA rails (front pair holed), dark rear panel."""
    W, H, D = mm(RACK["W"]), mm(RACK["H"]), mm(RACK["D"])
    fx, fy = W / 2.0, D / 2.0
    pc = powder_coat()
    dk = interior_dark()
    parts = []

    # 4 corner posts (30mm square section, full height)
    ps = mm(30.0)
    for sx in (-1, 1):
        for sy in (-1, 1):
            p = rounded_box("post", ps, ps, H, mm(2.0), seg=4)
            p.location = (sx * (fx - ps / 2), sy * (fy - ps / 2), H / 2.0)
            p.data.materials.append(pc); smooth(p, 30); parts.append(p)

    # top cap + base plinth
    cap = box("cap", W, D, mm(24.0), (0, 0, H - mm(12.0)))
    cap.data.materials.append(pc); parts.append(cap)
    plinth = box("plinth", W - mm(8), D - mm(8), mm(PLINTH), (0, 0, mm(PLINTH) / 2.0))
    plinth.data.materials.append(pc); parts.append(plinth)

    # two side channels (0U gutters): solid dark walls from rail-outer to cabinet wall
    for sx in (-1, 1):
        wall = box("sidewall", mm(2.0), D - mm(60), H - mm(PLINTH) - mm(30),
                   (sx * (fx - mm(2)), 0, mm(PLINTH) + (H - mm(PLINTH) - mm(30)) / 2.0))
        wall.data.materials.append(pc); parts.append(wall)

    # dark rear interior panel (what shows through empty bays · the depth read)
    back = box("back", W - mm(20), mm(3.0), H - mm(PLINTH) - mm(40),
               (0, fy - mm(20), mm(PLINTH) + (H - mm(PLINTH) - mm(40)) / 2.0))
    back.data.materials.append(dk); parts.append(back)

    # 4 EIA rails · front pair carries the square holes; rear pair plain
    rail_x = mm(HOLE_SPAN) / 2.0
    for sx in (-1, 1):
        r = rail_with_holes(sx * rail_x, f"rail-front-{sx}")
        r.data.materials.append(pc); parts.append(r)
        rr = box(f"rail-rear-{sx}", mm(30), mm(2), H - mm(PLINTH) - mm(40),
                 (sx * rail_x, fy - mm(120), mm(PLINTH) + (H - mm(PLINTH) - mm(40)) / 2.0))
        rr.data.materials.append(pc); parts.append(rr)
    return parts

# ---- rig · dark-object hero (edge + sheen carve the black out of void black) --------------
def add_area(name, loc, size, energy, color=(1, 1, 1), sx=None, aim=None):
    ld = bpy.data.lights.new(name, "AREA"); ld.energy = energy; ld.color = color
    if sx is not None:
        ld.shape = "RECTANGLE"; ld.size = sx; ld.size_y = size
    else:
        ld.size = size
    ob = bpy.data.objects.new(name, ld); ob.location = loc
    bpy.context.collection.objects.link(ob)
    if aim is not None:
        c = ob.constraints.new("TRACK_TO"); c.target = aim
        c.track_axis = "TRACK_NEGATIVE_Z"; c.up_axis = "UP_Y"
    return ob

def rack_rig():
    # A tall (~2m) dark object. Key camera-left high, a strong RIM behind-above drawing every
    # edge + rail, a frontal fill the satin sheen reflects. Void-black world, matte floor.
    # Energies scaled up for the metric ~2m object + the low (0.03) powder-coat albedo · these
    # are FIRST-PROBE values; the in-rig tone read (frame-front) tunes them (dark-object risk).
    aim = bpy.data.objects.new("Aim", None); aim.location = (0, 0, mm(950))
    bpy.context.collection.objects.link(aim)
    # PROVEN frame-probe values (gate 5a): powder-coat lands lit-face L~22 (key side), shadow
    # L~11, void-black separation clean, clip 0.000% peak 0.808. Refined per-part with node faces.
    add_area("key", (-1.6, -2.0, 2.6), 1.4, float(arg("--key", 460)), (1.0, 0.99, 0.97), aim=aim)
    add_area("rim", (1.2, 1.9, 2.5), 0.10, float(arg("--rim", 300)), (0.93, 0.96, 1.0), sx=1.8, aim=aim)
    add_area("fill", (0.2, -2.4, 1.1), 2.2, float(arg("--fill", 175)), (0.97, 0.98, 1.0), aim=aim)
    bpy.ops.mesh.primitive_plane_add(size=12.0, location=(0, 0, 0))
    fl = bpy.context.active_object; fl.name = "floor"
    fl.data.materials.append(principled("floor", (0.006, 0.006, 0.007), 0.62))
    return aim

def rack_camera(aim, shot, res):
    sc = bpy.context.scene
    cd = bpy.data.cameras.new("cam"); cd.lens = 70.0; cd.sensor_width = 36.0
    cam = bpy.data.objects.new("cam", cd); bpy.context.collection.objects.link(cam); sc.camera = cam
    H = mm(RACK["H"])
    dist = 4.6
    if shot in ("front", "frame-front"):
        yaw, elev = 0.0, 4.0
    else:  # q34
        yaw, elev = 32.0, 8.0
    ya, el = math.radians(yaw), math.radians(elev)
    ax, ay, az = aim.location
    cam.location = (ax + dist * math.cos(el) * math.sin(ya),
                    ay - dist * math.cos(el) * math.cos(ya),
                    az + dist * math.sin(el))
    c = cam.constraints.new("TRACK_TO"); c.target = aim
    c.track_axis = "TRACK_NEGATIVE_Z"; c.up_axis = "UP_Y"
    cd.dof.use_dof = True; cd.dof.focus_object = aim; cd.dof.aperture_fstop = 16.0
    sc.render.resolution_x, sc.render.resolution_y = res

def render_to(path):
    sc = bpy.context.scene; sc.render.filepath = path
    t0 = time.time(); bpy.ops.render.render(write_still=True)
    print(f"rendered {path} in {time.time()-t0:.1f}s ({sc.cycles.samples} spp, {sc.render.resolution_percentage}%)")

# ---- main --------------------------------------------------------------------------------
import os as _os
_os.makedirs(OUT, exist_ok=True)
sc = reset_scene(); enable_gpu(sc)
build_frame()
aim = rack_rig()
res = (1400, 2000) if SHOT in ("front", "frame-front") else (1800, 2000)
rack_camera(aim, SHOT, res)
render_to(OUT + f"frame-{SHOT}.png")
print("build_rack frame proof done.")
