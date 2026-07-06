# build_rack.py · the THIRD oracle: a person-owned 12U open-frame home GPU rig (on casters).
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
# archetype: OWNER REDIRECT 2026-07-05 · from the 42U NetShelter cabinet to a HOME GPU RIG ·
# a 12U open-frame rack on casters (~0.7m, waist-high · "some people could have it at home") ·
# holds an open row of 6 GPUs. 19in width kept (standard rails), depth + height cut to home
# scale, side/back panels + door dropped (OPEN frame · fans breathe, cards read).
RACK = dict(W=860.0, H=700.0, D=480.0, Ucount=12)
OPEN = True               # open 4-post frame (no side/back panels, no door hardware)
GPU_RIG = True            # 6-GPU open rig: cards hang from a top bar (no 19in rails), mobo tray + PSU
PANEL_W = 482.6           # 19in ear-to-ear
HOLE_SPAN = 465.12        # rail hole-center span
SQ_HOLE = 9.5             # square cage-nut hole
HOLE_OFF = (6.35, 22.25, 38.10)   # hole centers from each U boundary
PLINTH = 90.0             # base below U1 (700 - 12*44.45 = 166.6 total; plinth + cap + caster gap)
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

def switch_white(name="switch-white"):
    # CRS354 · the ONE bright face (material variety · L74 pin, cool white-grey · crs354_sth_front).
    # A faint peel like the powder, much higher albedo · reads bright on the dark-hero rig.
    m = principled(name, (0.535, 0.543, 0.560), 0.46, metallic=0.0)  # tuned to L74 pin (was 0.62, read L78.5)
    nt = m.node_tree; b = nt.nodes["Principled BSDF"]
    tc = nt.nodes.new("ShaderNodeTexCoord"); n = nt.nodes.new("ShaderNodeTexNoise")
    n.inputs["Scale"].default_value = 900.0 / S; n.inputs["Detail"].default_value = 2.0
    nt.links.new(tc.outputs["Object"], n.inputs["Vector"])
    bump = nt.nodes.new("ShaderNodeBump"); bump.inputs["Strength"].default_value = 0.05
    nt.links.new(n.outputs["Fac"], bump.inputs["Height"]); nt.links.new(bump.outputs["Normal"], b.inputs["Normal"])
    return add_bevel(m)

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

def build_side_panel(sx, pc):
    """A real cabinet side panel: recessed field + raised perimeter frame (edge returns that
    throw a shadow line at the post junction) + a quarter-turn slam latch near the front-top.
    Replaces the flat 2mm slab that read as a dead CGI wall at hero (q34) distance · the
    orange-peel powder micro-bump is sub-pixel there, so the READ has to come from geometry."""
    W, H, D = mm(RACK["W"]), mm(RACK["H"]), mm(RACK["D"])
    fx = W / 2.0; Ph = mm(PLINTH)
    z0, z1 = Ph, H - mm(30.0)                 # vertical extent (matches the old sidewall)
    hz = z1 - z0; zc = (z0 + z1) / 2.0
    dy = D - mm(60.0); yh = dy / 2.0          # depth extent
    b = mm(30.0)                              # perimeter frame width
    rim_x = sx * (fx - mm(2.5))              # rim outer face ~flush with the post outer face (±fx)
    fld_x = sx * (fx - mm(10.0))            # field recessed ~7mm behind the rim inner face
    parts = []
    fld = box("side-field", mm(2.0), dy - 2 * b, hz - 2 * b, (fld_x, 0, zc))
    fld.data.materials.append(pc); parts.append(fld)
    for nm, sy, sz, cy, cz in (
        ("top",   dy,        b,          0.0,        z1 - b / 2),
        ("bot",   dy,        b,          0.0,        z0 + b / 2),
        ("front", b,         hz - 2 * b, -yh + b / 2, zc),
        ("rear",  b,         hz - 2 * b,  yh - b / 2, zc)):
        r = box(f"side-rim-{nm}", mm(5.0), sy, sz, (rim_x, cy, cz))
        r.data.materials.append(pc); parts.append(r)
    latch = rounded_box("side-latch", mm(4.0), mm(20.0), mm(20.0), mm(3.0), seg=8)
    latch.location = (sx * (fx - mm(1.0)), -yh + b + mm(30.0), z1 - b - mm(80.0))
    latch.data.materials.append(principled("side-latch-mat", (0.30, 0.30, 0.32), 0.34, metallic=0.8))
    smooth(latch, 30); parts.append(latch)
    return parts

def build_door_hardware(fx, fy, H, Ph, pc):
    """R0.2 (the deferred half) · door-off frame hardware · 3 hinge bosses on the hinge-side
    front post + a latch keeper on the latch-side post · breaks the dead-straight post
    silhouette (the acceptance) and reads at 3/4 (q34), the pass this was held for. Proud of
    the front plane toward -Y, ahead of the mounted units where real door gear lives."""
    parts = []
    ff = -fy                                    # front-post front face plane (y)
    steel = principled("hinge-steel", (0.32, 0.32, 0.34), 0.36, metallic=0.8)
    hx = -(fx - mm(6.0))                        # hinge side = left front post, outboard edge
    for hz in (Ph + mm(250.0), H * 0.52, H - mm(320.0)):
        brk = rounded_box("hinge-boss", mm(16.0), mm(22.0), mm(38.0), mm(3.0), seg=4)
        brk.location = (hx, ff - mm(9.0), hz); brk.data.materials.append(pc)
        smooth(brk, 30); parts.append(brk)
        bpy.ops.mesh.primitive_cylinder_add(radius=mm(6.0), depth=mm(30.0), vertices=20,
                                            location=(hx - mm(4.0), ff - mm(20.0), hz))
        bar = bpy.context.active_object; bar.name = "hinge-barrel"
        bar.data.materials.append(steel); smooth(bar, 30); parts.append(bar)
    kx = (fx - mm(8.0))                         # latch side = right front post
    kp = rounded_box("latch-keeper", mm(14.0), mm(16.0), mm(44.0), mm(3.0), seg=4)
    kp.location = (kx, ff - mm(7.0), H * 0.5); kp.data.materials.append(pc)
    smooth(kp, 30); parts.append(kp)
    bpy.ops.mesh.primitive_cylinder_add(radius=mm(4.0), depth=mm(20.0), vertices=16,
                                        location=(kx - mm(3.0), ff - mm(14.0), H * 0.5))
    rl = bpy.context.active_object; rl.name = "latch-roller"
    rl.data.materials.append(steel); smooth(rl, 30); parts.append(rl)
    return parts

def build_frame():
    """12U OPEN home-rig frame (owner redirect): 4 posts, top cap, low base on 4 casters, four
    EIA rails (front pair holed). No side/back panels, no door (OPEN) so the 6-GPU row breathes
    and reads. The enclosed-cabinet path (side panels + door hardware) stays gated behind OPEN."""
    W, H, D = mm(RACK["W"]), mm(RACK["H"]), mm(RACK["D"])
    fx, fy = W / 2.0, D / 2.0
    pc = powder_coat()
    dk = interior_dark()
    parts = []

    # 4 corner posts · front FACE 45mm (0.075 of width, the real front vertical frame weight);
    # depth 32mm (C-profile front leg).
    psx = mm(45.0); psy = mm(32.0)
    for sx in (-1, 1):
        for sy in (-1, 1):
            p = rounded_box("post", psx, psy, H, mm(2.0), seg=4)
            p.location = (sx * (fx - psx / 2), sy * (fy - psy / 2), H / 2.0)
            p.data.materials.append(pc); smooth(p, 30); parts.append(p)

    if not OPEN:   # door-off hinge bosses + latch keeper · only the enclosed-cabinet archetype
        parts += build_door_hardware(fx, fy, H, mm(PLINTH), pc)

    # top cap + roof front band · W0.3 (audit: roof reads like a picture frame): a deeper 45mm
    # front band gives the capped, heavy-browed top real cabinets show.
    cap = box("cap", W, D, mm(24.0), (0, 0, H - mm(12.0)))
    cap.data.materials.append(pc); parts.append(cap)
    roofband = box("roofband", W, mm(6.0), mm(45.0), (0, -(fy - mm(3)), H - mm(22.5)))
    roofband.data.materials.append(pc); parts.append(roofband)

    # low base rail + 4 swivel casters · a rolling home rig (owner: "on casters"). FG is the
    # floor gap the wheels lift it to; the base rail sits just above, posts land on it.
    FG = mm(58.0)
    plinth = box("plinth", W - mm(20), D - mm(20), mm(24.0), (0, 0, FG + mm(12.0)))
    plinth.data.materials.append(pc); parts.append(plinth)
    wheel_mat = interior_dark("caster-wheel")
    mnt_mat = principled("caster-mount", (0.30, 0.30, 0.32), 0.36, metallic=0.8)
    for sx in (-1, 1):
        for sy in (-1, 1):
            cx0, cy0 = sx * (fx - mm(52)), sy * (fy - mm(52))
            mnt = rounded_box("caster-mount", mm(46), mm(46), mm(12), mm(3), seg=4)
            mnt.location = (cx0, cy0, FG + mm(6)); mnt.data.materials.append(mnt_mat)
            smooth(mnt, 30); parts.append(mnt)
            fork = rounded_box("caster-fork", mm(30), mm(11), mm(30), mm(3), seg=4)
            fork.location = (cx0, cy0 - mm(7), FG - mm(14)); fork.data.materials.append(mnt_mat)
            smooth(fork, 30); parts.append(fork)
            bpy.ops.mesh.primitive_cylinder_add(radius=FG / 2.2, depth=mm(20.0), vertices=28,
                                                location=(cx0, cy0, FG / 2.2),
                                                rotation=(0, math.radians(90), 0))
            whl = bpy.context.active_object; whl.name = "caster-wheel"
            whl.data.materials.append(wheel_mat); smooth(whl, 30); parts.append(whl)

    if not OPEN:   # side panels + rear panel · only the enclosed-cabinet archetype
        for sx in (-1, 1):
            parts += build_side_panel(sx, pc)
        back = box("back", W - mm(20), mm(3.0), H - mm(PLINTH) - mm(40),
                   (0, fy - mm(20), mm(PLINTH) + (H - mm(PLINTH) - mm(40)) / 2.0))
        back.data.materials.append(dk); parts.append(back)

    if not GPU_RIG:   # 4 EIA 19in rails · only the standard rackmount archetype (the GPU rig
        # hangs its cards from a top bar instead · see build_gpu_row).
        rail_x = mm(HOLE_SPAN) / 2.0
        front_ry = -(RACK["D"] / 2.0 - 20.0)
        for sx in (-1, 1):
            r = rail_with_holes(sx * rail_x, f"rail-front-{sx}", ry=front_ry)
            r.data.materials.append(pc); parts.append(r)
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

# ---- the 6-GPU rig (owner redirect · the hero content) ------------------------------------
def build_fan(cx, yf, cz, r):
    """One axial fan on a GPU front face (blows toward -Y, the viewer). Recessed dark well +
    bezel ring + hub + radial blades · the signature GPU read. yf = shroud front-face plane."""
    parts = []
    well = principled("fan-well", (0.015, 0.015, 0.017), 0.55)
    ring = principled("fan-ring", (0.03, 0.03, 0.033), 0.42, metallic=0.35)
    blade = principled("fan-blade", (0.06, 0.06, 0.066), 0.44, metallic=0.10)
    bpy.ops.mesh.primitive_cylinder_add(radius=r - mm(1.5), depth=mm(11.0), vertices=40,
        location=(cx, yf + mm(6.5), cz), rotation=(math.radians(90), 0, 0))
    w = bpy.context.active_object; w.name = "fan-well"; w.data.materials.append(well)
    smooth(w, 30); parts.append(w)
    bpy.ops.mesh.primitive_cylinder_add(radius=r, depth=mm(4.0), vertices=40,
        location=(cx, yf + mm(0.5), cz), rotation=(math.radians(90), 0, 0))
    rm = bpy.context.active_object; rm.name = "fan-rim"
    bpy.ops.mesh.primitive_cylinder_add(radius=r - mm(3.5), depth=mm(8.0), vertices=40,
        location=(cx, yf + mm(0.5), cz), rotation=(math.radians(90), 0, 0))
    inr = bpy.context.active_object
    bpy.context.view_layer.objects.active = rm
    md = rm.modifiers.new("h", "BOOLEAN"); md.operation = "DIFFERENCE"; md.solver = "EXACT"; md.object = inr
    bpy.ops.object.modifier_apply(modifier=md.name); bpy.data.objects.remove(inr, do_unlink=True)
    rm.data.materials.append(ring); smooth(rm, 30); parts.append(rm)
    bpy.ops.mesh.primitive_cylinder_add(radius=mm(13.0), depth=mm(9.0), vertices=28,
        location=(cx, yf + mm(2.0), cz), rotation=(math.radians(90), 0, 0))
    hb = bpy.context.active_object; hb.name = "fan-hub"; hb.data.materials.append(ring)
    smooth(hb, 30); parts.append(hb)
    nb = 7; rr = (mm(13.0) + r) / 2.0
    for i in range(nb):
        a = 2 * math.pi * i / nb
        bl = rounded_box("fan-blade", r - mm(17.0), mm(1.4), mm(15.0), mm(0.5), seg=2)
        bl.location = (cx + rr * math.cos(a), yf + mm(4.5), cz + rr * math.sin(a))
        bl.rotation_euler = (0, a, math.radians(20.0))
        bl.data.materials.append(blade); smooth(bl, 30); parts.append(bl)
    return parts

def build_gpu(cx, cz, yc, idx=0):
    """One triple-fan graphics card, PORTRAIT (standing) + fan-face forward (-Y). ~305mm tall ·
    the card the owner wants to SEE. Shroud + backplate + 3 fans + fin/power top + PCIe bracket
    and gold-finger stub at the bottom (riser mount). Per-index material so the row isn't clones."""
    Wc, Hc, Tc = mm(118.0), mm(285.0), mm(52.0)
    parts = []
    tint = 0.004 * ((idx % 3) - 1)   # tiny per-card shroud variance (not clones)
    shroud_mat = principled(f"gpu-shroud{idx}", (0.030 + tint, 0.030 + tint, 0.035 + tint), 0.40, metallic=0.45)
    plate_mat = principled(f"gpu-plate{idx}", (0.16, 0.16, 0.175), 0.34, metallic=0.85)
    dark = principled(f"gpu-dark{idx}", (0.02, 0.02, 0.022), 0.6)
    yf = yc - Tc / 2.0
    sh = rounded_box("gpu-shroud", Wc, Tc, Hc, mm(4.0), seg=4)
    sh.location = (cx, yc, cz); sh.data.materials.append(shroud_mat); smooth(sh, 30); parts.append(sh)
    bp = rounded_box("gpu-backplate", Wc - mm(3), mm(2.0), Hc - mm(4), mm(2.0), seg=3)
    bp.location = (cx, yc + Tc / 2.0 + mm(1.0), cz); bp.data.materials.append(plate_mat); smooth(bp, 30); parts.append(bp)
    # front-face relief · a raised perimeter lip + 2 inter-fan ridges make a recessed 3-fan
    # channel, so the shroud reads as a real card (not a flat slab) and the lips catch the key.
    lip_y = yf - mm(2.0)
    for nm, w, h, lx, lz in (
        ("lip-top", Wc, mm(9.0), cx, cz + Hc / 2.0 - mm(5.0)),
        ("lip-bot", Wc, mm(9.0), cx, cz - Hc / 2.0 + mm(5.0)),
        ("lip-l", mm(9.0), Hc, cx - Wc / 2.0 + mm(5.0), cz),
        ("lip-r", mm(9.0), Hc, cx + Wc / 2.0 - mm(5.0), cz)):
        lp = rounded_box("gpu-" + nm, w, mm(4.0), h, mm(1.5), seg=3)
        lp.location = (lx, lip_y, lz); lp.data.materials.append(shroud_mat); smooth(lp, 30); parts.append(lp)
    for dz in (mm(46.0), -mm(46.0)):
        rg = rounded_box("gpu-ridge", Wc - mm(22.0), mm(4.0), mm(7.0), mm(1.5), seg=2)
        rg.location = (cx, lip_y, cz + dz); rg.data.materials.append(shroud_mat); smooth(rg, 30); parts.append(rg)
    # 3 fans recessed in the channel
    for dz in (mm(92.0), 0.0, -mm(92.0)):
        parts += build_fan(cx, yf, cz + dz, mm(40.0))
    fins = box("gpu-fins", Wc - mm(8), Tc - mm(8), mm(8.0), (cx, yc, cz + Hc / 2.0 + mm(3.0)))
    fins.data.materials.append(dark); parts.append(fins)
    pwr = rounded_box("gpu-pwr", mm(26.0), mm(16.0), mm(11.0), mm(1.5), seg=2)
    pwr.location = (cx + Wc / 4.0, yc + mm(4.0), cz + Hc / 2.0 + mm(10.0)); pwr.data.materials.append(dark)
    smooth(pwr, 30); parts.append(pwr)
    # bottom · dark PCB edge + dark PCIe riser connector · NO bright bracket (it read as a white
    # band). The gold fingers sit INSIDE the riser slot, barely seen · risers get wired at 5.x.
    pcb = box("gpu-pcb", Wc - mm(4), Tc - mm(14), mm(7.0), (cx, yc, cz - Hc / 2.0 - mm(3.0)))
    pcb.data.materials.append(principled(f"gpu-pcb{idx}", (0.02, 0.028, 0.022), 0.6)); parts.append(pcb)
    riser = box("gpu-riser", mm(94.0), mm(22.0), mm(16.0), (cx, yc, cz - Hc / 2.0 - mm(13.0)))
    riser.data.materials.append(principled(f"gpu-riser{idx}", (0.03, 0.03, 0.033), 0.5)); parts.append(riser)
    return parts

def build_gpu_row():
    """6 GPUs hung from a top mounting bar, fan-faces forward · a mobo tray + PSU at the base
    (the riser-mounted open-rig look). Owner spec: 'open row of 6 cards, fans out'."""
    W, H, D = mm(RACK["W"]), mm(RACK["H"]), mm(RACK["D"])
    fx, fy = W / 2.0, D / 2.0
    parts = []
    n = 6; pitch = mm(124.0)
    yc = -fy + mm(110.0)
    cz = mm(PLINTH) + mm(300.0)
    x0 = -pitch * (n - 1) / 2.0
    bar = box("gpu-mountbar", pitch * (n - 1) + mm(150), mm(30.0), mm(28.0),
              (0, yc + mm(6.0), cz + mm(285.0) / 2.0 + mm(20.0)))
    bar.data.materials.append(principled("gpu-bar", (0.055, 0.055, 0.062), 0.38, metallic=0.8)); parts.append(bar)
    for i in range(n):
        parts += build_gpu(x0 + i * pitch, cz, yc, idx=i)
    tray = box("mobo-tray", W - mm(160), D - mm(120), mm(4.0), (0, mm(10.0), mm(PLINTH) + mm(95.0)))
    tray.data.materials.append(principled("mobo-tray-mat", (0.055, 0.055, 0.062), 0.55, metallic=0.3)); parts.append(tray)
    psu = rounded_box("psu", mm(150.0), mm(86.0), mm(150.0), mm(3.0), seg=3)
    psu.location = (-fx + mm(150), mm(30.0), mm(PLINTH) + mm(63.0))
    psu.data.materials.append(principled("psu-mat", (0.035, 0.035, 0.04), 0.40, metallic=0.4)); smooth(psu, 30); parts.append(psu)
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
    # Home GPU rig (~0.7m). Key camera-left high, a RIM behind-above drawing every edge + rail,
    # a frontal fill the satin + GPU shrouds reflect. Void-black world, matte floor. Positions +
    # energies scaled DOWN from the 2m-cabinet rig (~half distance -> ~quarter energy for equal
    # illuminance) · FIRST-PROBE for the open rig (brighter GPU faces) · eyeball + clip-gate tune.
    aim = bpy.data.objects.new("Aim", None); aim.location = (0, 0, mm(RACK["H"] / 2.0))
    bpy.context.collection.objects.link(aim)
    add_area("key", (-0.8, -1.0, 1.15), 0.7, float(arg("--key", 72)), (1.0, 0.99, 0.97), aim=aim)
    add_area("rim", (0.6, 0.95, 1.1), 0.06, float(arg("--rim", 52)), (0.93, 0.96, 1.0), sx=0.9, aim=aim)
    add_area("fill", (0.1, -1.2, 0.55), 1.1, float(arg("--fill", 16)), (0.97, 0.98, 1.0), aim=aim)
    bpy.ops.mesh.primitive_plane_add(size=8.0, location=(0, 0, 0))
    fl = bpy.context.active_object; fl.name = "floor"
    fl.data.materials.append(principled("floor", (0.006, 0.006, 0.007), 0.62))
    return aim

def rack_camera(aim, shot, res):
    sc = bpy.context.scene
    cd = bpy.data.cameras.new("cam"); cd.lens = 70.0; cd.sensor_width = 36.0
    cam = bpy.data.objects.new("cam", cd); bpy.context.collection.objects.link(cam); sc.camera = cam
    H = mm(RACK["H"])
    dist = 2.5    # wide (~0.86m) 6-GPU rig · was 4.6 for the 2m cabinet
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

# ---- CRS354 switch · Wave 2 · the white 1U unit (material variety) · crs354_sth_front.jpg -----
CRS = dict(W=443.0, D=297.0, Hb=44.3, EARW=482.6)

def build_crs354_switch(cx=0.0, cz=0.0):
    """Wave 2.1 · chassis 443x297x44.3 WHITE powder + rack ears (+~20mm/side to 482.6) +
    faceplate seam. Port grid / cages / LEDs land on 2.2-2.4."""
    W, D, Hb = mm(CRS["W"]), mm(CRS["D"]), mm(CRS["Hb"])
    sw = switch_white()
    parts = []
    body = rounded_box("crs-body", W, D, Hb, mm(1.5), seg=3)
    body.location = (cx, 0, cz + Hb / 2.0); body.data.materials.append(sw); smooth(body, 30); parts.append(body)
    fy = -D / 2.0
    ear_ext = mm((CRS["EARW"] - CRS["W"]) / 2.0)
    for sx in (-1, 1):
        ear = rounded_box("crs-ear", ear_ext, mm(2.0), Hb - mm(3), mm(1.0), seg=3)
        ear.location = (sx * (W / 2.0 + ear_ext / 2.0), fy + mm(1.0), cz + Hb / 2.0)
        ear.data.materials.append(sw); smooth(ear, 30); parts.append(ear)
    # faceplate seam · a thin recessed line ~4mm below the top edge (the front bezel split)
    seam = box("crs-seam", W - mm(4), mm(1.0), mm(0.6), (cx, fy + mm(0.3), cz + Hb - mm(6)))
    seam.data.materials.append(principled("crs-seam-mat", (0.30, 0.31, 0.33), 0.5)); parts.append(seam)
    # Wave 2.2 · 48 RJ45 ports = 4 GANGS of 2x6, recessed cavities (desktop USB-C treatment · dark
    # wall + AO interior + a lit lower contact) · the grid sits LEFT, leaving the right for the
    # SFP/QSFP cages (2.3). One joined cutter -> boolean once (not 48 booleans).
    pw, ph = mm(12.0), mm(10.5)                     # RJ45 aperture
    col_pitch, row_pitch = mm(13.6), mm(14.0); gang_gap = mm(7.0)
    z_top = cz + Hb * 0.66; z_bot = cz + Hb * 0.34
    grid_left = cx - mm(200.0)                       # grid starts left of center
    cav_mat = principled("rj45-cavity", (0.055, 0.055, 0.060), 0.62, metallic=0.1)
    contact_mat = principled("rj45-contact", (0.55, 0.47, 0.20), 0.4, metallic=0.7)   # gold pins
    cutters = []; contacts = []; centers = []
    for gang in range(4):
        gx = grid_left + gang * (6 * col_pitch + gang_gap)
        for col in range(6):
            x = gx + col * col_pitch
            for zc in (z_top, z_bot):
                cutters.append(box("pc", pw, mm(16), ph, (x, fy + mm(5), zc)))
                centers.append((x, zc))
    # join cutters into one mesh, boolean once. DESELECT first: bpy.ops.object.join() merges
    # EVERY selected mesh into the active one · a caller may leave unrelated objects selected
    # (e.g. assembly place_y leaves the last-placed node selected), which join would swallow +
    # then remove with `joined`. Own the selection state here.
    bpy.ops.object.select_all(action="DESELECT")
    bpy.context.view_layer.objects.active = cutters[0]
    for c in cutters[1:]: c.select_set(True)
    cutters[0].select_set(True); bpy.ops.object.join()
    joined = cutters[0]
    md = body.modifiers.new("ports", "BOOLEAN"); md.operation = "DIFFERENCE"; md.solver = "EXACT"; md.object = joined
    bpy.context.view_layer.objects.active = body; bpy.ops.object.modifier_apply(modifier=md.name)
    bpy.data.objects.remove(joined, do_unlink=True)
    # dark interior slab just behind the face + a gold contact bar low in each port
    inter = box("crs-interior", W - mm(20), mm(2), Hb - mm(6), (cx, fy + mm(7), cz + Hb / 2.0))
    inter.data.materials.append(cav_mat); parts.append(inter)
    for (x, zc) in centers:
        cb = box("rj45-pin", pw - mm(2.5), mm(1.2), mm(1.6), (x, fy + mm(3.0), zc - ph/2 + mm(2.0)))
        cb.data.materials.append(contact_mat); contacts.append(cb)
    parts += contacts
    # Wave 2.3 · SFP+ (4, 2x2) + QSFP+ (2, stacked) cages · dark-nickel recessed, right of the grid.
    nickel = principled("cage-nickel", (0.10, 0.10, 0.11), 0.42, metallic=0.8)
    cage_cut = []; cage_lips = []
    sfp_x0 = grid_left + 4 * (6 * col_pitch + gang_gap) + mm(6)
    for i in range(2):
        for zc in (z_top, z_bot):
            x = sfp_x0 + i * mm(15.0)
            cage_cut.append(box("sfpc", mm(13.0), mm(16), mm(11.5), (x, fy + mm(5), zc)))
            lip = box("sfp-lip", mm(13.6), mm(1.2), mm(12.1), (x, fy + mm(0.6), zc)); lip.data.materials.append(nickel); cage_lips.append(lip)
    qsfp_x0 = sfp_x0 + mm(34.0)
    for zc in (z_top + mm(1), z_bot - mm(1)):
        cage_cut.append(box("qsfpc", mm(20.0), mm(16), mm(13.0), (qsfp_x0, fy + mm(5), zc)))
        lip = box("qsfp-lip", mm(20.8), mm(1.2), mm(13.8), (qsfp_x0, fy + mm(0.6), zc)); lip.data.materials.append(nickel); cage_lips.append(lip)
    bpy.ops.object.select_all(action="DESELECT")
    bpy.context.view_layer.objects.active = cage_cut[0]
    for c in cage_cut[1:]: c.select_set(True)
    cage_cut[0].select_set(True); bpy.ops.object.join(); jc = cage_cut[0]
    md2 = body.modifiers.new("cages", "BOOLEAN"); md2.operation = "DIFFERENCE"; md2.solver = "EXACT"; md2.object = jc
    bpy.context.view_layer.objects.active = body; bpy.ops.object.modifier_apply(modifier=md2.name)
    bpy.data.objects.remove(jc, do_unlink=True)
    parts += cage_lips
    return parts

def switch_rig_camera(shot, res):
    aim = bpy.data.objects.new("Aim", None); aim.location = (0, 0, mm(CRS["Hb"]) / 2.0)
    bpy.context.collection.objects.link(aim)
    # white unit · brighter albedo, so the dark-object energies land it near the L74 pin
    add_area("key", (-0.5, -0.7, 0.7), 0.4, float(arg("--key", 14.0)), (1.0, 0.99, 0.97), aim=aim)
    add_area("rim", (0.4, 0.6, 0.7), 0.04, float(arg("--rim", 9.0)), (0.93, 0.96, 1.0), sx=0.5, aim=aim)
    add_area("fill", (0.0, -0.8, 0.3), 0.7, float(arg("--fill", 5.3)), (0.97, 0.98, 1.0), aim=aim)
    bpy.ops.mesh.primitive_plane_add(size=6.0, location=(0, 0, 0))
    fl = bpy.context.active_object; fl.data.materials.append(principled("floor", (0.006, 0.006, 0.007), 0.62))
    sc = bpy.context.scene
    cd = bpy.data.cameras.new("cam"); cd.lens = 85.0; cd.sensor_width = 36.0
    cam = bpy.data.objects.new("cam", cd); bpy.context.collection.objects.link(cam); sc.camera = cam
    dist = 1.9
    yaw, elev = (0.0, 4.0) if shot == "front" else (30.0, 12.0)
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
elif PART == "switch":
    build_crs354_switch()
    switch_rig_camera(SHOT, (1900, 900))
    render_to(OUT + f"switch-{SHOT}.png")
    print("build_rack CRS354 switch proof done.")
elif PART == "gpurig":
    # THE HOME GPU RIG (owner redirect) · open frame + a row of 6 GPUs, fans out.
    build_frame()
    build_gpu_row()
    aim = rack_rig()
    res = (2000, 1500) if SHOT in ("front", "frame-front") else (2000, 1500)
    rack_camera(aim, SHOT, res)
    render_to(OUT + f"gpurig-{SHOT}.png")
    print("build_rack GPU RIG (6 cards) done.")
elif PART == "assembly":
    # Wave 5 · POPULATED rack · place the built units by u_z() into the frame (fill map v2, the
    # subset built so far: 3 nodes + switch · empty bays show the rails). One rig for the whole
    # 2m rack (the frame rig · per-object-class tone · dark powder + the one white switch).
    build_frame()
    def place_y(parts, dy):
        bpy.ops.object.select_all(action="DESELECT")
        for p in parts:
            try: p.select_set(True)
            except Exception: pass
        bpy.ops.transform.translate(value=(0, dy, 0))
    NODE_DY, SW_DY = -0.253, -0.339      # align each unit FRONT to the front-rail plane (~-0.487)
    for uu in (5, 10, 15):
        place_y(build_rm44_node(0.0, u_z(uu) / 1000.0), NODE_DY)
    place_y(build_crs354_switch(0.0, u_z(38) / 1000.0), SW_DY)
    if arg("--debug", False):
        for o in bpy.data.objects:
            if o.type != "MESH": continue
            if not any(k in o.name for k in ("rm44", "door", "crs", "body", "ear", "interior")): continue
            zs = [(o.matrix_world @ v.co).z for v in o.data.vertices]
            ys = [(o.matrix_world @ v.co).y for v in o.data.vertices]
            print(f"[dbg] {o.name:22} z[{min(zs):.3f},{max(zs):.3f}] y[{min(ys):.3f},{max(ys):.3f}]")
    aim = rack_rig()
    res = (1500, 1650) if SHOT in ("front", "frame-front") else (1900, 1550)
    rack_camera(aim, SHOT, res)
    render_to(OUT + f"assembly-{SHOT}.png")
    print("build_rack ASSEMBLY (populated) done.")
else:
    build_frame()
    aim = rack_rig()
    res = (1500, 1650) if SHOT in ("front", "frame-front") else (1900, 1550)
    rack_camera(aim, SHOT, res)
    render_to(OUT + f"frame-{SHOT}.png")
    print("build_rack frame proof done.")
