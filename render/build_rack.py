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
    # W0.6 · authentic orange-peel = a VORONOI cell layer (the characteristic peel dimples) over
    # the fine noise · two scales give the real spatially-varying satin break-up that a single
    # noise lacked. Albedo + roughness CENTER unchanged (tone gate holds · verified dE2.08).
    peel = nt.nodes.new("ShaderNodeTexVoronoi"); peel.feature = "F1"
    peel.inputs["Scale"].default_value = 520.0 / S
    nt.links.new(tc.outputs["Object"], peel.inputs["Vector"])
    mixb = nt.nodes.new("ShaderNodeMixRGB"); mixb.inputs["Fac"].default_value = 0.55
    nt.links.new(n.outputs["Fac"], mixb.inputs["Color1"])
    nt.links.new(peel.outputs["Distance"], mixb.inputs["Color2"])
    bump = nt.nodes.new("ShaderNodeBump"); bump.inputs["Strength"].default_value = 0.10
    bump.inputs["Distance"].default_value = mm(0.04)
    nt.links.new(mixb.outputs["Color"], bump.inputs["Height"])
    nt.links.new(bump.outputs["Normal"], b.inputs["Normal"])
    return add_bevel(m)

def interior_dark(name="interior"):
    # cavity interior · albedo NEVER 0 so wall gradients read (desktop wave-2 lesson).
    return principled(name, (0.020, 0.020, 0.022), 0.80, metallic=0.0)

# ---- the enclosure frame -----------------------------------------------------------------
def rail_with_holes(x, name, ry=-490.0):
    """One EIA rail: a 1U flange segment with 3 square holes, arrayed x42. Placed at world
    (x, ry). The square-hole pattern is THE recognizable rack signature · hole size + per-U
    offsets are exact (MEASUREMENTS). ry selects front (-490) or rear rail plane (W0.4)."""
    flange_w, thick = mm(30.0), mm(2.0)
    seg = box(name + "-seg", flange_w, thick, mm(U), (x, mm(ry), u_z(1) / 1000.0 + mm(U) / 2.0))
    # cut 3 square holes through the Y thickness at the measured offsets
    cutters = []
    for off in HOLE_OFF:
        cz = u_z(1) / 1000.0 + mm(off)
        c = box("hcut", mm(SQ_HOLE), thick * 4, mm(SQ_HOLE), (x, mm(ry), cz))
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

    # 4 corner posts · R0.2 (geo-audit S3): front FACE widened 30 -> 45mm (0.075 of 600mm width,
    # the real NetShelter front vertical frame weight); depth kept 32mm (C-profile front leg).
    psx = mm(45.0); psy = mm(32.0)
    for sx in (-1, 1):
        for sy in (-1, 1):
            p = rounded_box("post", psx, psy, H, mm(2.0), seg=4)
            p.location = (sx * (fx - psx / 2), sy * (fy - psy / 2), H / 2.0)
            p.data.materials.append(pc); smooth(p, 30); parts.append(p)

    # R0.2 note · hinge bosses + latch keeper (door-off hardware, audit S2) DEFERRED to a
    # dedicated closer-shot box: at dead-front frame distance they sit edge-on and do not read,
    # and getting their proud-of-face Y right belongs with the 3/4 door-hardware pass. The post
    # FACE WIDENING (the graded S3 win) stands alone here · one clean measurable change.

    # top cap + roof front band · W0.3 (audit: roof reads like a picture frame): a deeper 45mm
    # front band gives the capped, heavy-browed top real cabinets show.
    cap = box("cap", W, D, mm(24.0), (0, 0, H - mm(12.0)))
    cap.data.materials.append(pc); parts.append(cap)
    roofband = box("roofband", W, mm(6.0), mm(45.0), (0, -(fy - mm(3)), H - mm(22.5)))
    roofband.data.materials.append(pc); parts.append(roofband)

    # base valance RAISED off the floor + 4 leveling feet + 2 casters · W0.3 (audit: "extruded
    # from the floor"). The valance bottom lifts to FLOOR_GAP=32mm; feet/casters fill the reveal
    # so a real dark floor gap reads head-on. Frame u_z unchanged (base structure stays; only the
    # visible valance + feet are added below the existing geometry).
    FG = mm(32.0)
    plinth = box("plinth", W - mm(8), D - mm(8), mm(PLINTH) - FG, (0, 0, FG + (mm(PLINTH) - FG) / 2.0))
    plinth.data.materials.append(pc); parts.append(plinth)
    foot_mat = principled("foot-steel", (0.35, 0.35, 0.37), 0.35, metallic=0.8)
    for sx in (-1, 1):
        for sy in (-1, 1):
            ft = rounded_box("foot", mm(42.0), mm(42.0), FG, mm(6.0), seg=6)
            ft.location = (sx * (fx - mm(40)), sy * (fy - mm(40)), FG / 2.0)
            ft.data.materials.append(foot_mat); smooth(ft, 30); parts.append(ft)
        cst = rounded_box("caster", mm(50.0), mm(24.0), FG - mm(4), mm(10.0), seg=8)
        cst.location = (sx * (fx - mm(150)), -(fy - mm(60)), (FG - mm(4)) / 2.0)
        cst.data.materials.append(interior_dark("caster-rubber")); smooth(cst, 30); parts.append(cst)

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
        # W0.4 · rear rail DEFERRED to the assembly wave: making it holed at the shared rail x put
        # its dark see-through holes directly behind the front rail, crashing the powder_black
        # median (L18->L7) · the fragile perforated-flange patch cannot survive it, and the durable
        # patch (RM44 solid face) does not exist yet. On the POPULATED rack the rear rail reads
        # between real units and the patch lives on a unit · correct place for this. Plain bar for now.
        rr = box(f"rail-rear-{sx}", mm(30), mm(2), H - mm(PLINTH) - mm(40),
                 (sx * rail_x, fy - mm(120), mm(PLINTH) + (H - mm(PLINTH) - mm(40)) / 2.0))
        rr.data.materials.append(pc); parts.append(rr)

    # W0.4 · corner gusset 'castle' plates · small perforated brackets tying post to top/base,
    # visible at all four post ends in the refs · breaks the bare post-to-cap junction.
    for sx in (-1, 1):
        for zc in (mm(PLINTH) + mm(30), H - mm(60)):
            gus = rounded_box("gusset", mm(40.0), mm(30.0), mm(50.0), mm(3.0), seg=4)
            gus.location = (sx * (fx - psx / 2), -(fy - psy - mm(8)), zc)
            gus.data.materials.append(pc); smooth(gus, 30); parts.append(gus)
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

# ---- RM44 4U GPU node · the hero unit (Wave 1) · ref node/rm44_front_A.jpg ----------------
RM44 = dict(W=440.0, D=468.0, Hb=176.0, EARW=482.6, EAR_T=2.0)   # body + ear-tip width (EIA 19in)
MESH_P, MESH_R = 2.87, 2.59      # measured triangular lattice (mm · rm44 FFT autocorr, MEASUREMENTS)
MESH_SHRINK, MESH_THICK = 0.24, 1.2

def _tri_prism(name, side, depth, up=True, loc=(0, 0, 0)):
    me = bpy.data.meshes.new(name); bm = bmesh.new()
    hgt = side * math.sqrt(3) / 2.0; s = 1.0 if up else -1.0
    pts = [(-side/2.0, -s*hgt/2.0), (side/2.0, -s*hgt/2.0), (0.0, s*hgt/2.0)]
    vs = [bm.verts.new((mm(px), -mm(depth)/2.0, mm(pz))) for (px, pz) in pts]
    fce = bm.faces.new(vs)
    r = bmesh.ops.extrude_face_region(bm, geom=[fce])
    for gg in r["geom"]:
        if isinstance(gg, bmesh.types.BMVert): gg.co.y += mm(depth)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:]); bm.to_mesh(me); bm.free()
    ob = bpy.data.objects.new(name, me); bpy.context.collection.objects.link(ob); ob.location = loc
    bpy.context.view_layer.update(); return ob

def build_mesh_door(cx, cz, fy, field_w, field_h):
    """Wave 1.2 · the triangular-perforation mesh door · REAL cut holes (bake-off verdict, locked).
    One cell (P x 2R, up+down triangle boolean) arrayed across the field · cached like foam3d
    (heavy array). Placed at the front plane fy · the holes read through to the dark interior."""
    import os as _os
    key = f"mesh_w{field_w:.0f}h{field_h:.0f}p{MESH_P}r{MESH_R}s{MESH_SHRINK}t{MESH_THICK}"
    cdir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "cache"); _os.makedirs(cdir, exist_ok=True)
    cpath = _os.path.join(cdir, f"{key}.blend")
    if _os.path.exists(cpath):
        with bpy.data.libraries.load(cpath) as (src, dst): dst.meshes = list(src.meshes)[:1]
        door = bpy.data.objects.new("rm44-door", dst.meshes[0]); bpy.context.collection.objects.link(door)
        door.location = (cx, fy, cz); print(f"[mesh] cache hit ({len(door.data.polygons)} tris)")
        return door
    cw, ch = mm(MESH_P), mm(2 * MESH_R)
    me = bpy.data.meshes.new("cell"); bm = bmesh.new(); bmesh.ops.create_cube(bm, size=1.0)
    for v in bm.verts: v.co.x *= cw; v.co.y *= mm(MESH_THICK); v.co.z *= ch
    bm.to_mesh(me); bm.free()
    cell = bpy.data.objects.new("cell", me); bpy.context.collection.objects.link(cell)
    side = MESH_P - 2 * MESH_SHRINK
    for up, zc in ((True, mm(MESH_R/2)), (False, -mm(MESH_R/2))):
        c = _tri_prism("c", side, MESH_THICK*4, up=up, loc=(0, 0, zc))
        bpy.context.view_layer.objects.active = cell
        md = cell.modifiers.new("b", "BOOLEAN"); md.operation = "DIFFERENCE"; md.solver = "EXACT"; md.object = c
        bpy.ops.object.modifier_apply(modifier=md.name); bpy.data.objects.remove(c, do_unlink=True)
    nx, nz = int(mm(field_w)/cw), int(mm(field_h)/ch)
    for ax, off in (("ax", (cw, 0, 0)), ("az", (0, 0, ch))):
        a = cell.modifiers.new(ax, "ARRAY"); a.count = nx if ax == "ax" else nz
        a.use_relative_offset = False; a.use_constant_offset = True; a.constant_offset_displace = off
        bpy.ops.object.modifier_apply(modifier=ax)
    cell.location = (0, 0, 0)
    bbx = [cell.matrix_world @ __import__("mathutils").Vector(c) for c in cell.bound_box]
    ctr = sum((v for v in bbx), __import__("mathutils").Vector()) / 8.0
    for v in cell.data.vertices: v.co -= cell.matrix_world.inverted() @ ctr
    for p in cell.data.polygons: p.use_smooth = False
    try:
        bpy.data.libraries.write(cpath, {cell.data}, compress=True); print(f"[mesh] cached {cpath}")
    except Exception as e: print(f"[mesh] cache write failed: {e}")
    cell.name = "rm44-door"; cell.location = (cx, fy, cz)
    print(f"[mesh] {nx}x{nz} cells, {len(cell.data.polygons)} tris")
    return cell

def build_rm44_node(cx=0.0, cz=0.0):
    """Wave 1.1 · body + rack ears + 2 thumbscrews per ear. Front face toward -Y. The mesh door
    (1.2) + badge/lock (1.4) + interior (1.3) land on later boxes · this is the chassis silhouette,
    the foundation the ladder (RACK-DETAIL-AUDIT sec 2) builds on."""
    W, D, Hb = mm(RM44["W"]), mm(RM44["D"]), mm(RM44["Hb"])
    pc = powder_coat("node-powder")
    parts = []
    body = rounded_box("rm44-body", W, D, Hb, mm(2.5), seg=4)
    body.location = (cx, 0, cz + Hb / 2.0)
    body.data.materials.append(pc); smooth(body, 30)
    fy = -D / 2.0
    # Wave 1.2 · cut the door WINDOW in the body front (~8mm border frame) so the mesh reads
    # through to a dark interior · then the dark interior box · then the perforated mesh door.
    border = mm(8.0); field_w = RM44["W"] - 16.0; field_h = RM44["Hb"] - 16.0
    win = box("door-win", mm(field_w), mm(30), mm(field_h), (cx, fy + mm(3), cz + Hb / 2.0))
    bpy.context.view_layer.objects.active = body
    md = body.modifiers.new("win", "BOOLEAN"); md.operation = "DIFFERENCE"; md.solver = "EXACT"; md.object = win
    bpy.ops.object.modifier_apply(modifier=md.name); bpy.data.objects.remove(win, do_unlink=True)
    parts.append(body)
    interior = box("rm44-interior", W - mm(10), D - mm(20), Hb - mm(10),
                   (cx, mm(4), cz + Hb / 2.0))
    interior.data.materials.append(interior_dark("rm44-interior-mat")); parts.append(interior)
    door = build_mesh_door(cx, cz + Hb / 2.0, fy + mm(1.5), field_w, field_h)
    door.data.materials.append(powder_coat("door-powder")); parts.append(door)
    # Wave 1.4 · center LOCK (cylinder body + bail/wing handle · rm44_front_A) + keystone BADGE
    # plate (proud, chevron bottom, BLANK per trademark gate L6) · both proud of the mesh at
    # face-center, upper-middle · the door's dip zone where the real unit carries them.
    lock_z = cz + Hb * 0.60
    bpy.ops.mesh.primitive_cylinder_add(radius=mm(6.5), depth=mm(4.0), vertices=28,
                                        location=(cx, fy - mm(2.0), lock_z), rotation=(math.radians(90), 0, 0))
    lock = bpy.context.active_object; lock.name = "rm44-lock"
    lock.data.materials.append(principled("lock-satin", (0.05, 0.05, 0.055), 0.34, metallic=0.5))
    smooth(lock, 30); parts.append(lock)
    bail = rounded_box("rm44-bail", mm(22.0), mm(3.0), mm(5.0), mm(2.0), seg=4)   # wing handle
    bail.location = (cx, fy - mm(3.5), lock_z); bail.data.materials.append(lock.data.materials[0])
    smooth(bail, 30); parts.append(bail)
    badge = rounded_box("rm44-badge", mm(34.0), mm(1.5), mm(12.0), mm(2.0), seg=4)   # keystone plate (blank)
    badge.location = (cx, fy - mm(0.4), cz + Hb * 0.44); badge.data.materials.append(pc)
    smooth(badge, 30); parts.append(badge)
    # rack EARS · thin folded flanges at the front extending the width to 482.6 (each ear tip
    # (482.6-440)/2 = 21.3mm proud of the body side), full node height, front-mounted.
    ear_ext = mm((RM44["EARW"] - RM44["W"]) / 2.0)
    fy = -D / 2.0
    for sx in (-1, 1):
        ear = rounded_box("rm44-ear", ear_ext, mm(RM44["EAR_T"]), Hb - mm(6), mm(1.0), seg=3)
        ear.location = (sx * (W / 2.0 + ear_ext / 2.0), fy + mm(RM44["EAR_T"]) / 2.0, cz + Hb / 2.0)
        ear.data.materials.append(pc); smooth(ear, 30); parts.append(ear)
        # 2 knurled thumbscrews per ear (proud discs · U-boundary rows)
        for uz in (0.22, 0.78):
            ts = rounded_box("rm44-thumb", mm(11.0), mm(6.0), mm(11.0), mm(3.0), seg=6)
            ts.location = (sx * (W / 2.0 + ear_ext / 2.0), fy - mm(3.0), cz + uz * Hb)
            ts.data.materials.append(principled("thumb-zinc", (0.40, 0.40, 0.42), 0.34, metallic=0.85))
            smooth(ts, 30); parts.append(ts)
    return parts

def node_rig_camera(shot, res):
    """Solo-node rig: reuse the dark-object hero energies, re-aim at the node center (~88mm)."""
    aim = bpy.data.objects.new("Aim", None); aim.location = (0, 0, mm(RM44["Hb"]) / 2.0)
    bpy.context.collection.objects.link(aim)
    # Wave 1.5 · dark-object calibration · the first-guess energies lit the powder to L54 (grey) ·
    # a black server on the hero rig should read ~L20 lit-face (the frame-proof value) so the mesh
    # WEB is dark powder and the HOLES fall darker still (depth into the interior). Cut ~7x.
    add_area("key", (-0.55, -0.7, 0.9), 0.5, float(arg("--key", 14.0)), (1.0, 0.99, 0.97), aim=aim)
    add_area("rim", (0.45, 0.7, 0.85), 0.04, float(arg("--rim", 9.0)), (0.93, 0.96, 1.0), sx=0.6, aim=aim)
    add_area("fill", (0.0, -0.9, 0.4), 0.8, float(arg("--fill", 5.3)), (0.97, 0.98, 1.0), aim=aim)
    bpy.ops.mesh.primitive_plane_add(size=6.0, location=(0, 0, 0))
    fl = bpy.context.active_object; fl.name = "floor"
    fl.data.materials.append(principled("floor", (0.006, 0.006, 0.007), 0.62))
    sc = bpy.context.scene
    cd = bpy.data.cameras.new("cam"); cd.lens = 85.0; cd.sensor_width = 36.0
    cam = bpy.data.objects.new("cam", cd); bpy.context.collection.objects.link(cam); sc.camera = cam
    dist = 1.9   # node is 482mm wide x 176mm tall (2.7:1) · width drives the framing at 85mm
    yaw, elev = (0.0, 3.0) if shot == "front" else (32.0, 10.0)
    ya, el = math.radians(yaw), math.radians(elev)
    ax, ay, az = aim.location
    cam.location = (ax + dist*math.cos(el)*math.sin(ya), ay - dist*math.cos(el)*math.cos(ya), az + dist*math.sin(el))
    c = cam.constraints.new("TRACK_TO"); c.target = aim; c.track_axis = "TRACK_NEGATIVE_Z"; c.up_axis = "UP_Y"
    cd.dof.use_dof = True; cd.dof.focus_object = aim; cd.dof.aperture_fstop = 11.0
    sc.render.resolution_x, sc.render.resolution_y = res
    return aim

# ---- main --------------------------------------------------------------------------------
import os as _os
_os.makedirs(OUT, exist_ok=True)
sc = reset_scene(); enable_gpu(sc)
PART = str(arg("--part", "frame"))
if PART == "node":
    build_rm44_node()
    node_rig_camera(SHOT, (1800, 1400))
    render_to(OUT + f"node-{SHOT}.png")
    print("build_rack RM44 node proof done.")
else:
    build_frame()
    aim = rack_rig()
    res = (1400, 2000) if SHOT in ("front", "frame-front") else (1800, 2000)
    rack_camera(aim, SHOT, res)
    render_to(OUT + f"frame-{SHOT}.png")
    print("build_rack frame proof done.")
