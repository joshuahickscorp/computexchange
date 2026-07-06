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
POST = bool(arg("--post", False))   # hero-only compositor: subtle fog-glow bloom on the lit rings +
# vignette. Applied AFTER the numeric gate (the gate always reads the raw, pre-post frame).
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
RACK = dict(W=960.0, H=700.0, D=480.0, Ucount=12)
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
    sc.cycles.sample_clamp_indirect = 16.0   # was 8.0 · panel-3 (all lenses): the LEDs deposited zero GI
    # spill · the 8.0 indirect clamp was SUPPRESSING the emitter bounce onto the shroud/blades. Raise it
    # so the accurate LEDs actually wash cool light onto the neighbouring metal · OIDN handles fireflies.
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

def bevel_mod(ob, width=1.6, segs=2):
    """Small GEOMETRY bevel on a box's sharp edges (panel-6 #3: razor-CAD frame edges) · a real
    steel-tube chamfer that catches a thin highlight line, beyond what the shading-only bevel gives."""
    bpy.context.view_layer.objects.active = ob
    bm = ob.modifiers.new("bev", "BEVEL"); bm.width = mm(width); bm.segments = segs
    bm.limit_method = "ANGLE"; bm.angle_limit = math.radians(40)
    try: bm.clamp_overlap = True   # attribute absent on some Blender builds · bevel still applies without it
    except (AttributeError, TypeError): pass
    bpy.ops.object.modifier_apply(modifier=bm.name); smooth(ob, 40)
    return ob

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
    # large-scale (low-freq) roughness ZONING on top of the fine orange-peel · panel-6 #3: the frame
    # read as one uniform matte value · real powder-coated steel varies satin/matte across a face.
    # Roughness-only (albedo untouched · tone gate holds).
    lf = nt.nodes.new("ShaderNodeTexNoise"); lf.inputs["Scale"].default_value = 3.2; lf.inputs["Detail"].default_value = 2.0
    nt.links.new(tc.outputs["Object"], lf.inputs["Vector"])
    lfr = nt.nodes.new("ShaderNodeMapRange"); lfr.inputs["To Min"].default_value = -0.06; lfr.inputs["To Max"].default_value = 0.09
    nt.links.new(lf.outputs["Fac"], lfr.inputs["Value"])
    radd = nt.nodes.new("ShaderNodeMath"); radd.operation = "ADD"; radd.use_clamp = True
    nt.links.new(mapr.outputs["Result"], radd.inputs[0]); nt.links.new(lfr.outputs["Result"], radd.inputs[1])
    nt.links.new(radd.outputs["Value"], b.inputs["Roughness"])
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
    # AUTOPSY (panel-5 #6 · up-facing dust · REVERTED): a Z-normal dust mask lifting the base toward a
    # grey dust colour over-lightened the frame HORIZONTALS (top cap, mid-shelf, top rail) · under the
    # brightened key they read as a grey material change, not subtle dust · cheapened the premium
    # void-black frame. The 'clean premium' law wins · do NOT re-add visible frame dust on this hero.
    return add_bevel(m, radius=0.7)   # was 0.20 · panel-4: razor-sharp mitered edges read CG · a 0.7mm shading bevel lets the frame tube edges catch a thin specular highlight line (real powder-coated steel has a small edge radius)

def machined_metal(name, base, rough, metallic=0.9):
    """Real machined/anodized aluminium · a fine micro-bump (machining grain) + spatially-varying
    roughness so the surface reads as metal-with-microtexture, not a perfect CG plane (the #1
    photoreal tell · a flawless surface is what screams 'render'). Subtle · albedo unchanged."""
    m = principled(name, base, rough, metallic=metallic)
    nt = m.node_tree; b = nt.nodes["Principled BSDF"]
    tc = nt.nodes.new("ShaderNodeTexCoord")
    nr = nt.nodes.new("ShaderNodeTexNoise")
    nr.inputs["Scale"].default_value = 13.0; nr.inputs["Detail"].default_value = 3.0
    nt.links.new(tc.outputs["Object"], nr.inputs["Vector"])
    mr = nt.nodes.new("ShaderNodeMapRange")
    mr.inputs["To Min"].default_value = max(0.05, rough - 0.07); mr.inputs["To Max"].default_value = rough + 0.07
    nt.links.new(nr.outputs["Fac"], mr.inputs["Value"])
    # large-scale HANDLING smudge (panel tell #4: uniform roughness = 'clay'). A soft cm-scale
    # blotch that adds/subtracts a little roughness so the key light glances off the flat faces
    # UNEVENLY, like real handled hardware · albedo untouched (keeps the premium dark look).
    sm = nt.nodes.new("ShaderNodeTexNoise")
    sm.inputs["Scale"].default_value = 6.5; sm.inputs["Detail"].default_value = 3.0   # dm-cm handling blotches
    nt.links.new(tc.outputs["Object"], sm.inputs["Vector"])
    smr = nt.nodes.new("ShaderNodeMapRange")
    smr.inputs["To Min"].default_value = -0.06; smr.inputs["To Max"].default_value = 0.085
    nt.links.new(sm.outputs["Fac"], smr.inputs["Value"])
    radd = nt.nodes.new("ShaderNodeMath"); radd.operation = "ADD"; radd.use_clamp = True
    nt.links.new(mr.outputs["Result"], radd.inputs[0]); nt.links.new(smr.outputs["Result"], radd.inputs[1])
    nt.links.new(radd.outputs["Value"], b.inputs["Roughness"])
    nb = nt.nodes.new("ShaderNodeTexNoise")
    nb.inputs["Scale"].default_value = 680.0; nb.inputs["Detail"].default_value = 2.0
    nt.links.new(tc.outputs["Object"], nb.inputs["Vector"])
    bump = nt.nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.04; bump.inputs["Distance"].default_value = mm(0.02)
    nt.links.new(nb.outputs["Fac"], bump.inputs["Height"]); nt.links.new(bump.outputs["Normal"], b.inputs["Normal"])
    return add_bevel(m, radius=0.35)   # rounded-shading edges catch a bright micro-bevel highlight (real metal)

def floor_mat(name="floor"):
    """Near-black studio floor that GROUNDS the rig via a soft reflection · panel-4 #4: a perfect
    uniform mirror reads CG. A large-scale smudge noise varies the roughness (0.13 sharp patches ->
    0.42 hazed patches) so the reflection locally blurs like a real handled floor, not a mirror."""
    m = principled(name, (0.006, 0.006, 0.007), 0.20, metallic=0.0)
    nt = m.node_tree; b = nt.nodes["Principled BSDF"]
    tc = nt.nodes.new("ShaderNodeTexCoord")
    n = nt.nodes.new("ShaderNodeTexNoise"); n.inputs["Scale"].default_value = 2.6; n.inputs["Detail"].default_value = 2.0
    nt.links.new(tc.outputs["Object"], n.inputs["Vector"])
    mr = nt.nodes.new("ShaderNodeMapRange"); mr.inputs["To Min"].default_value = 0.13; mr.inputs["To Max"].default_value = 0.42
    nt.links.new(n.outputs["Fac"], mr.inputs["Value"]); nt.links.new(mr.outputs["Result"], b.inputs["Roughness"])
    return m

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
            p.data.materials.append(pc); bevel_mod(p, width=1.2); parts.append(p)   # bevel the post top/bottom edges too · softens the post-cap/plinth junction (panel-6 #3)

    if not OPEN:   # door-off hinge bosses + latch keeper · only the enclosed-cabinet archetype
        parts += build_door_hardware(fx, fy, H, mm(PLINTH), pc)

    # top cap + roof front band · W0.3 (audit: roof reads like a picture frame): a deeper 45mm
    # front band gives the capped, heavy-browed top real cabinets show.
    cap = box("cap", W, D, mm(24.0), (0, 0, H - mm(12.0)))
    cap.data.materials.append(pc); bevel_mod(cap); parts.append(cap)
    roofband = box("roofband", W, mm(6.0), mm(45.0), (0, -(fy - mm(3)), H - mm(22.5)))
    roofband.data.materials.append(pc); parts.append(roofband)

    # low base rail + 4 swivel casters · a rolling home rig (owner: "on casters"). FG is the
    # floor gap the wheels lift it to; the base rail sits just above, posts land on it.
    FG = mm(58.0)
    plinth = box("plinth", W - mm(20), D - mm(20), mm(24.0), (0, 0, FG + mm(12.0)))
    plinth.data.materials.append(pc); bevel_mod(plinth); parts.append(plinth)
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

# ---- the 6-GPU rig · NVIDIA RTX 5090 Founders Edition (see render/ref/rack/RTX5090FE-SPEC.md) --
def emissive(name, color, strength):
    """A constant emitter · the 5090 FE's static white illumination (wordmark / X accents / inlet
    rings). Handles both the new (Emission Color/Strength) and legacy (Emission) socket names."""
    m = bpy.data.materials.new(name); m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*color, 1)
    try:
        b.inputs["Emission Color"].default_value = (*color, 1)
        b.inputs["Emission Strength"].default_value = strength
    except KeyError:
        try:
            b.inputs["Emission"].default_value = (*color, 1)
            b.inputs["Emission Strength"].default_value = strength
        except KeyError:
            pass
    # subtle along-length intensity ripple so the LED strip isn't a perfectly uniform tube · a real
    # diffused LED bar shows faint per-LED brightness variation (panel-4 #2). Modulates around the same
    # supra-white level (clip-neutral).
    try:
        nt = m.node_tree; tc = nt.nodes.new("ShaderNodeTexCoord")
        nz = nt.nodes.new("ShaderNodeTexNoise"); nz.inputs["Scale"].default_value = 46.0; nz.inputs["Detail"].default_value = 2.0
        nt.links.new(tc.outputs["Object"], nz.inputs["Vector"])
        mr = nt.nodes.new("ShaderNodeMapRange")
        mr.inputs["To Min"].default_value = strength * 0.80; mr.inputs["To Max"].default_value = strength * 1.12
        nt.links.new(nz.outputs["Fac"], mr.inputs["Value"])
        nt.links.new(mr.outputs["Result"], b.inputs["Emission Strength"])
    except Exception as e:
        print("emissive ripple skip:", e)
    return m

def _fan_blades(cx, yf, cz, r, nb, blade_mat):
    """One lofted-airfoil bladed rotor as a SINGLE watertight mesh (panel tell #3: flat paddles).
    Each blade is a real cambered foil lofted across 6 span stations: chord taper (root->mid bulge->
    tip), root-to-tip twist (high angle-of-attack at the hub, shallow at the tip), and a sickle
    sweep (the leading edge trails back). Blades overlap by ~1.4x their pitch so you cannot see
    straight through to the well · fan axis = +Y, disc plane = world X-Z, pitched to blow toward -Y."""
    me = bpy.data.meshes.new("fan-rotor"); bm = bmesh.new()
    r0   = mm(12.0)          # root radius · roots tuck UNDER the 14mm hub cap (was 15 = a visible gap ring / hard seam · panel-3 #3)
    rtip = r - mm(1.5)       # tip radius · G2: extended out so the tips REACH the rim ring (was r-3.0 = a 3mm floating-tip gap to the bezel/LED ring · real FE tips fuse into the ring)
    Nsp  = 6                 # span stations (root -> tip)
    Nc   = 7                 # chord samples (leading -> trailing edge)
    y0   = yf + mm(3.5)      # blade mid-plane depth (recessed behind the inlet rim)
    pitch_ang = 2 * math.pi / nb
    def chord_of(t, rad):
        base = 1.42 * pitch_ang * rad                       # ~1.4x the blade pitch -> overlap
        return base * (0.72 + 0.42 * math.sin(math.pi * (0.12 + 0.76 * t)))  # root->mid bulge->tip taper
    def airfoil(u):          # u in [-0.5,0.5] chord fraction -> (mean-camber, half-thickness), chord-fractions
        s = max(1.0 - (2.0 * u) ** 2, 0.0)                  # 1 at mid-chord, 0 at the edges
        return 0.17 * s, 0.082 * s ** 0.7                   # more camber + thicker lens · panel-5 #2: 0.055 read as a flat plate · thicker foils catch a light gradient across the curve
    cs = [-0.5 + k / (Nc - 1) for k in range(Nc)]           # leading -> trailing
    for i in range(nb):
        a0 = 2 * math.pi * i / nb
        rings = []
        for sidx in range(Nsp):
            t = sidx / (Nsp - 1)
            rad = r0 + (rtip - r0) * t
            phi = a0 + 0.30 * (t ** 1.3)                    # GENTLE sickle sweep · panel-3 read 0.52 as too-curled 'scythe' blades · real FE blades are shallow-swept
            pitch = math.radians(38.0 - 22.0 * t)           # twist: steep AoA at root -> shallow at tip
            chord = chord_of(t, rad)
            cp, sp = math.cos(pitch), math.sin(pitch)
            tx, tz = -math.sin(phi), math.cos(phi)          # tangential (chord) unit in the X-Z disc plane
            bx, bz = cx + rad * math.cos(phi), cz + rad * math.sin(phi)
            pts = [(u * chord, (c + h) * chord) for u in cs for c, h in [airfoil(u)]]          # upper surface
            pts += [(u * chord, (c - h) * chord) for u in reversed(cs[1:-1]) for c, h in [airfoil(u)]]  # lower
            ring = [bm.verts.new((bx + tx * (al * cp), y0 + al * sp + yaf, bz + tz * (al * cp)))
                    for al, yaf in pts]
            rings.append(ring)
        P = len(rings[0])
        for sidx in range(Nsp - 1):
            A, B = rings[sidx], rings[sidx + 1]
            for k in range(P):
                k2 = (k + 1) % P
                bm.faces.new((A[k], A[k2], B[k2], B[k]))
        bm.faces.new(tuple(rings[0]))                       # cap root
        bm.faces.new(tuple(reversed(rings[-1])))            # cap tip
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(me); bm.free()
    ob = bpy.data.objects.new("fan-rotor", me); bpy.context.collection.objects.link(ob)
    ob.data.materials.append(blade_mat); smooth(ob, 40)
    return ob

def build_fan(cx, yf, cz, r, nb=9, blade_rgb=(0.105, 0.105, 0.115), emit_ring=False):
    """One axial fan on a GPU front face (blows toward -Y, the viewer). Recessed dark well +
    bezel ring + hub + hub sticker + nb broad swept blades that nearly fill the disc. yf = shroud
    front-face plane. emit_ring adds the 5090 FE's cool-white lit inlet ring around the fan."""
    parts = []
    well = principled("fan-well", (0.012, 0.012, 0.014), 0.55)
    ring = machined_metal("fan-ring", (0.03, 0.03, 0.033), 0.42, metallic=0.5)   # machined-metal bezel/rim/hub · same micro-texture + edge bevel as the shroud (was flat principled) · consistent metal read around each fan
    blade = principled("fan-blade", blade_rgb, 0.44, metallic=0.0, coat=0.30)   # glossy-ish black ABS (panel-6 #1: dead matte 'clay') · lower rough + a tight clearcoat so a NARROW bright spec streak travels the blade (real molded fan plastic) · the LED glow is a light object now, not the emissive flood that greyed it
    # roughness micro-texture + bump so the blade faces catch a MOVING highlight, not one flat value
    # (panel-6 #1: 'no micro-normal detail') · object-space so it rides the geometry.
    _bt = blade.node_tree; _bb = _bt.nodes["Principled BSDF"]
    _btc = _bt.nodes.new("ShaderNodeTexCoord")
    _bn = _bt.nodes.new("ShaderNodeTexNoise"); _bn.inputs["Scale"].default_value = 220.0; _bn.inputs["Detail"].default_value = 3.0
    _bt.links.new(_btc.outputs["Object"], _bn.inputs["Vector"])
    _bmr = _bt.nodes.new("ShaderNodeMapRange"); _bmr.inputs["To Min"].default_value = 0.32; _bmr.inputs["To Max"].default_value = 0.54
    _bt.links.new(_bn.outputs["Fac"], _bmr.inputs["Value"]); _bt.links.new(_bmr.outputs["Result"], _bb.inputs["Roughness"])
    _bfn = _bt.nodes.new("ShaderNodeTexNoise"); _bfn.inputs["Scale"].default_value = 320.0; _bfn.inputs["Detail"].default_value = 2.0
    _bt.links.new(_btc.outputs["Object"], _bfn.inputs["Vector"])
    _bbmp = _bt.nodes.new("ShaderNodeBump"); _bbmp.inputs["Strength"].default_value = 0.10; _bbmp.inputs["Distance"].default_value = mm(0.03)
    _bt.links.new(_bfn.outputs["Fac"], _bbmp.inputs["Height"]); _bt.links.new(_bbmp.outputs["Normal"], _bb.inputs["Normal"])
    bpy.ops.mesh.primitive_cylinder_add(radius=r - mm(1.5), depth=mm(26.0), vertices=44,
        location=(cx, yf + mm(14.0), cz), rotation=(math.radians(90), 0, 0))
    w = bpy.context.active_object; w.name = "fan-well"; w.data.materials.append(well)
    smooth(w, 30); parts.append(w)
    # DEEP dark heatsink fins filling the well behind the blades (panel-4 #1: the fan reads as a solid
    # disc on a flat lit gray plate · a real FE is a deep FINNED cavity you look INTO). Vertical fins,
    # each sized to the circular chord, set back so the blade gaps reveal dark receding fin depth.
    fin_dark = principled("fan-hsfin", (0.008, 0.008, 0.010), 0.62, metallic=0.55)   # near-black · renders darker than the lit blades so the gaps read as DEEP cavity, not a gray plate
    nf = 17; rr_in = r - mm(4.0)
    for k in range(nf):
        fxr = (2.0 * (k + 0.5) / nf - 1.0) * rr_in
        half_h = math.sqrt(max(rr_in * rr_in - fxr * fxr, 0.0))
        if half_h < mm(3): continue
        fin = box("fan-hsfin", mm(0.8), mm(18.0), 2.0 * half_h, (cx + fxr, yf + mm(13.0), cz))
        fin.data.materials.append(fin_dark); parts.append(fin)
    bpy.ops.mesh.primitive_cylinder_add(radius=r, depth=mm(4.0), vertices=44,
        location=(cx, yf + mm(0.5), cz), rotation=(math.radians(90), 0, 0))
    rm = bpy.context.active_object; rm.name = "fan-rim"
    bpy.ops.mesh.primitive_cylinder_add(radius=r - mm(3.5), depth=mm(8.0), vertices=44,
        location=(cx, yf + mm(0.5), cz), rotation=(math.radians(90), 0, 0))
    inr = bpy.context.active_object
    bpy.context.view_layer.objects.active = rm
    md = rm.modifiers.new("h", "BOOLEAN"); md.operation = "DIFFERENCE"; md.solver = "EXACT"; md.object = inr
    bpy.ops.object.modifier_apply(modifier=md.name); bpy.data.objects.remove(inr, do_unlink=True)
    rm.data.materials.append(ring); smooth(rm, 30); parts.append(rm)
    # nb lofted-airfoil blades (real cambered/twisted/swept foils · one watertight mesh) · panel tell #3
    parts.append(_fan_blades(cx, yf, cz, r, nb, blade))
    # BLADE-TIP RIM RING · G2 (GRADING-REPORT): the real FE fan fuses all blade tips into a smooth
    # outer ring band ("a ring around it" · LanOC). Thin band at the tip radius (mirrors _fan_blades
    # rtip = r-1.5) so the blades read as a ring-fan, not free-floating paddles. A dark GLOSSY grey (not
    # the near-black blade plastic) so the band catches a subtle rim sheen + reads distinct from the well.
    tipr = r - mm(1.5)
    ring_blk = principled("fan-tipring-mat", (0.055, 0.057, 0.064), 0.34, metallic=0.0, coat=0.5)
    bpy.ops.mesh.primitive_torus_add(major_radius=tipr, minor_radius=mm(1.4),
        location=(cx, yf + mm(3.2), cz), rotation=(math.radians(90), 0, 0),
        major_segments=64, minor_segments=12)
    trg = bpy.context.active_object; trg.name = "fan-tipring"
    trg.scale = (1.0, 0.60, 1.0); bpy.ops.object.transform_apply(scale=True)   # flatten toward a band, not a round tube
    trg.data.materials.append(ring_blk); smooth(trg, 40); parts.append(trg)
    bpy.ops.mesh.primitive_cylinder_add(radius=mm(14.0), depth=mm(9.0), vertices=28,
        location=(cx, yf + mm(1.5), cz), rotation=(math.radians(90), 0, 0))
    hb = bpy.context.active_object; hb.name = "fan-hub"; hb.data.materials.append(ring)
    smooth(hb, 30); parts.append(hb)
    bpy.ops.mesh.primitive_uv_sphere_add(radius=mm(9.0), segments=24, ring_count=12,
        location=(cx, yf + mm(1.0), cz))
    dome = bpy.context.active_object; dome.name = "fan-hubcap"
    dome.scale = (1.0, 0.12, 1.0); bpy.ops.object.transform_apply(scale=True)   # G3: FLAT cap (0.30->0.12) · real FE hub is flat + plain, not a raised dome
    dome.data.materials.append(principled("fan-hubcap", (0.042, 0.042, 0.047), 0.34, metallic=0.4))   # G3: darker + less glossy (rough 0.18->0.34) · a plain dark-grey cap, still catches a soft LED spec
    smooth(dome, 40); parts.append(dome)
    if emit_ring:   # the 5090 FE lit inlet ring (static cool white)
        bpy.ops.mesh.primitive_torus_add(major_radius=r + mm(1.0), minor_radius=mm(0.6),
            location=(cx, yf - mm(0.5), cz), rotation=(math.radians(90), 0, 0),
            major_segments=48, minor_segments=8)
        er = bpy.context.active_object; er.name = "fan-inlet-lit"
        er.data.materials.append(emissive("fan-inlet-lit-mat", (0.86, 0.92, 1.0), 9.0))  # true LED · THIN light-guide line (minor 1.3->0.6): real FE inlet rings are thin lines · thinner supra-white ring cuts the clipped-pixel AREA under the 1% gate · emission 9 held, raised indirect clamp preserves the cool spill
        smooth(er, 30); parts.append(er)
        # co-located cool-white LIGHT object (panel-3/4/5 #1: the emissive ring casts near-zero near-field
        # GI). A light is NOT a camera-visible surface, so it adds the soft cool halo on the shroud + a rim
        # on the blades + spill to neighbours WITHOUT adding a single clipped pixel · resolves the gate-vs-
        # spill tension the emissive-only ring could not. Modest power · gate-checked.
        gl = bpy.data.lights.new("led-glow", "POINT"); gl.energy = float(arg("--ledglow", 0.32))   # 0.32 = the balance · fans read LIT (hub glow + shroud halo + blade rim) but the black blades stay dark, not washed grey
        gl.color = (0.80, 0.89, 1.0); gl.shadow_soft_size = mm(28.0)
        go = bpy.data.objects.new("led-glow", gl); go.location = (cx, yf - mm(7.0), cz)
        bpy.context.collection.objects.link(go); parts.append(go)
    return parts

def build_gpu(cx, cz, yc, idx=0):
    """NVIDIA RTX 5090 Founders Edition · PORTRAIT (standing), fan-face forward (-Y). Dark Gun
    Metal monochrome, 137w x 304h x 40t (2-slot). Two ~115mm 7-blade fans at the two ends over
    flow-through fin stacks, an X 'infinity' accent between them, static cool-white lit inlet
    rings + X + a top-edge wordmark. Bracket + angled 16-pin + backplate. render/ref/rack/RTX5090FE-SPEC.md."""
    Wc, Hc, Tc = mm(137.0), mm(304.0), mm(40.0)
    parts = []
    body_mat = machined_metal(f"fe-body{idx}", (0.128, 0.135, 0.150), 0.42, metallic=0.9)   # gunmetal · glossier still (0.60->0.50->0.42, monotonic toward the panel's ~0.4 ask) · a tight bright die-cast-metal spec that clearly reads as METAL vs the matte frame + matte blades (material identity · panel-5 #5)
    fin_mat = principled(f"fe-fin{idx}", (0.030, 0.032, 0.038), 0.50, metallic=0.9)   # darker · the finstack backing behind the fans was a lit mid-gray 'plate' (panel-4 #1) · now a dark cavity backing
    xacc_mat = principled(f"fe-x{idx}", (0.14, 0.15, 0.16), 0.40, metallic=0.9)
    plate_mat = machined_metal(f"fe-plate{idx}", (0.10, 0.104, 0.113), 0.45, metallic=0.9)
    brk_mat = principled(f"fe-brk{idx}", (0.078, 0.078, 0.078), 0.50, metallic=0.9)
    dark = principled(f"fe-dark{idx}", (0.02, 0.02, 0.022), 0.6)
    lit = emissive(f"fe-lit{idx}", (0.86, 0.92, 1.0), 7.0)   # true LED · lights the shroud (temper 12->7 with the rings)
    yf = yc - Tc / 2.0
    fan_r = mm(57.0)                          # ~115mm dia
    zt, zb = cz + mm(78.0), cz - mm(78.0)     # the two fan centers (one at each end)
    # shroud as a FRAME: perimeter border + a center bar · the two ends are the open flow-through
    # zones (fin stacks + fans fill them). bw = border width.
    bw = mm(11.0)
    for nm, w, h, lx, lz in (
        ("edge-top", Wc, bw, cx, cz + Hc / 2.0 - bw / 2),
        ("edge-bot", Wc, bw, cx, cz - Hc / 2.0 + bw / 2),
        ("edge-l", bw, Hc, cx - Wc / 2.0 + bw / 2, cz),
        ("edge-r", bw, Hc, cx + Wc / 2.0 - bw / 2, cz)):
        eb = rounded_box("fe-" + nm, w, Tc, h, mm(4.0), seg=4)
        eb.location = (lx, yc, lz); eb.data.materials.append(body_mat); smooth(eb, 30); parts.append(eb)
    # center module · G4 (GRADING-REPORT): the real FE two-tone is a BLACK center section with a
    # gunmetal OUTER frame + X (club386, LanOC) · the builder had the center in gunmetal body_mat
    # (inverted). Black die-cast center now, so the gunmetal X + lit strips pop against it.
    center_mat = machined_metal(f"fe-center{idx}", (0.034, 0.036, 0.042), 0.46, metallic=0.85)
    cbar = rounded_box("fe-centerbar", Wc, Tc, mm(48.0), mm(3.0), seg=3)
    cbar.location = (cx, yc, cz); cbar.data.materials.append(center_mat); smooth(cbar, 30); parts.append(cbar)
    # flow-through fin stack behind each fan · G7 (GRADING-REPORT): the real FE rear windows reveal the
    # fin EDGES receding into the card, dished concave where the fan sits ("vertical heatsink fins that
    # are all black" · LanOC; "concave right where the fans are located"). Vertical black fins with gaps,
    # occupying the REAR HALF of the thickness (yc+7 .. yc+19) so they show through the rear window while
    # the front fan well (ends ~yc+7) still reads from the front · true double flow-through.
    for fz in (zt, zb):
        nrf = 23; span = fan_r + mm(4)
        for k in range(nrf):
            fxr = (2.0 * (k + 0.5) / nrf - 1.0) * span
            half_h = math.sqrt(max((fan_r + mm(2)) ** 2 - fxr * fxr, 0.0))   # circular concave profile
            if half_h < mm(4): continue
            rf = box("fe-flowfin", mm(0.9), mm(12.0), 2.0 * half_h, (cx + fxr, yc + mm(13.0), fz))
            rf.data.materials.append(fin_mat); parts.append(rf)
        # a dark floor a bit deeper so the gaps between fins read as a receding cavity, not open sky
        back = box("fe-finfloor", Wc - 2 * bw, mm(2.0), fan_r * 2, (cx, yc + mm(6.0), fz))
        back.data.materials.append(principled(f"fe-finfloor{idx}", (0.010, 0.010, 0.012), 0.6)); parts.append(back)
    # exhaust fin comb on the TOP + BOTTOM short-ends · the aluminium fin edges the FE vents through
    # (reads from the top/bottom views · thin ridges across the width, spaced along the depth).
    for ez, zend in ((cz + Hc / 2.0 - mm(1.0), 1), (cz - Hc / 2.0 + mm(1.0), -1)):
        for k in range(-6, 7):
            fr = box("fe-topfin", Wc - 2 * bw - mm(4), mm(2.0), mm(3.0), (cx, yc + k * mm(2.7), ez))
            fr.data.materials.append(fin_mat); parts.append(fr)
    # two fans (7 blades, BLACK plastic) with the FE lit inlet rings. Reviews call the FE fans flatly
    # "black" · 0.110 read medium-grey · 0.050 is a proper black plastic (glossy coat keeps the spec/
    # edge highlights so the blades still read against the dark well). RTX5090FE-SPEC.md.
    # BLADE COUNT · G1 (GRADING-REPORT): real FE fans have SEVEN large wide-chord blades, not 9 · the
    # chord auto-widens 9/7 = 1.28x since chord_of scales with the 2pi/nb pitch. Sources: LanOC
    # ("7 large axial blades"), club386 ("seven thick blades"), Overclocking.com.
    for fz in (zt, zb):
        parts += build_fan(cx, yf, fz, fan_r, nb=7, blade_rgb=(0.050, 0.052, 0.060), emit_ring=True)
    # X 'infinity' accent · G5 (GRADING-REPORT): ONE flush integrated X (union the two diagonals) so the
    # crossing is FLUSH, not a stacked-bar step; arms extended (78->86) so the tips reach toward the fan
    # rims, forming the continuous 'infinity loop' with the lit rings. Gunmetal on the black center module.
    xbars = []
    for sgn in (1, -1):
        xb = rounded_box("fe-xbar", mm(86.0), mm(3.0), mm(10.0), mm(2.0), seg=2)
        xb.location = (cx, yf - mm(2.0), cz); xb.rotation_euler = (0, math.radians(sgn * 33.0), 0)
        xbars.append(xb)
    bpy.context.view_layer.objects.active = xbars[0]
    mu = xbars[0].modifiers.new("u", "BOOLEAN"); mu.operation = "UNION"; mu.solver = "EXACT"; mu.object = xbars[1]
    bpy.ops.object.modifier_apply(modifier=mu.name); bpy.data.objects.remove(xbars[1], do_unlink=True)
    xbars[0].name = "fe-xacc"; xbars[0].data.materials.append(xacc_mat); smooth(xbars[0], 30); parts.append(xbars[0])
    # lit strips inset along the X arms · thin, converging at the centre (club386 "converging in the centre")
    for sgn in (1, -1):
        xl = box("fe-xlit", mm(86.0), mm(1.0), mm(0.9), (cx, yf - mm(3.6), cz))   # thin lit strip · kept thin under the 1% clip gate
        xl.rotation_euler = (0, math.radians(sgn * 33.0), 0); xl.data.materials.append(lit); parts.append(xl)
    # small co-located glow at the X centre · the X emissive lines cast no near-field GI (like the rings
    # did) · a dim light makes the X spill onto the centre bar too (consistent with the ring-glow fix,
    # panel-5 #1). Low power (0.14W) · the centre bar is already partly lit by the two ring glows.
    xg = bpy.data.lights.new("x-glow", "POINT"); xg.energy = 0.14; xg.color = (0.80, 0.89, 1.0)
    xg.shadow_soft_size = mm(24.0)
    xgo = bpy.data.objects.new("x-glow", xg); xgo.location = (cx, yf - mm(7.0), cz)
    bpy.context.collection.objects.link(xgo); parts.append(xgo)
    # top-edge wordmark · a blank lit strip on the 'top edge' (a vertical side edge when portrait,
    # +X so it faces the q34 camera), plus the angled recessed 16-pin power header near it
    # top-edge GEFORCE RTX wordmark · G9 (GRADING-REPORT): a THIN backlit text-height strip on the
    # central third of the top edge (blank per trademark gate), not the old 18x70mm glowing tile that
    # blew out the side view. Backlit cool white, flush-inset. Sources: club386/TheFPSReview (backlit).
    wm = box("fe-wordmark", mm(1.2), mm(4.5), mm(54.0), (cx + Wc / 2.0 - mm(0.6), yc, cz + mm(6.0)))
    wm.data.materials.append(lit); parts.append(wm)
    # 16-pin 12V-2x6 power · G10 (GRADING-REPORT): a RECESSED, angled socket on the top edge, not a
    # protruding cylinder (HotHardware/club386/LanOC: "recessed... angled ~45deg" toward the tail). NO
    # cable on the beauty card (the FE product shots have none · the rig gets proper draped cabling in R1).
    pex = cx + Wc / 2.0                     # +X top-edge outer plane
    pz  = cz + mm(96.0)                     # toward the tail end (away from the bracket), clear of the wordmark
    ang = math.radians(-32.0)              # tilt the mouth up-and-out toward the tail
    hous = rounded_box("fe-pwr-house", mm(11.0), mm(13.0), mm(27.0), mm(2.0), seg=2)   # gunmetal scallop housing
    hous.location = (pex - mm(5.0), yc, pz); hous.rotation_euler = (0, ang, 0)
    hous.data.materials.append(body_mat); smooth(hous, 30); parts.append(hous)
    sock = box("fe-pwr-sock", mm(8.0), mm(9.0), mm(23.0), (pex - mm(8.0), yc, pz))     # dark recessed cavity
    sock.rotation_euler = (0, ang, 0); sock.data.materials.append(dark); parts.append(sock)
    pin = box("fe-pwr-pins", mm(1.2), mm(3.0), mm(21.0), (pex - mm(6.5), yc, pz))      # faint 12+4 pin ridge
    pin.rotation_euler = (0, ang, 0); pin.data.materials.append(plate_mat); parts.append(pin)
    # backplate (dark metal) · a mirrored rear X 'infinity' accent + a blank etched cartouche
    # (NVIDIA logo · blank per trademark gate) + a large flow-through WINDOW over the top fan
    # (air exits the fin stack out the back here · the FE's defining rear feature).
    # backplate · G7: TWO flow-through windows (one per fan/end), not one · the FE's defining rear
    # feature is the open double flow-through revealing the fin stacks. Cut both over zt AND zb.
    by = yc + Tc / 2.0 + mm(1.0)
    bp = box("fe-backplate", Wc - mm(4), mm(2.5), Hc - mm(6), (cx, by, cz))
    bpy.context.view_layer.objects.active = bp
    for wi, wz in enumerate((zt, zb)):
        win = box(f"fe-bpwin{wi}", mm(100.0), mm(20.0), mm(100.0), (cx, by, wz))
        mdw = bp.modifiers.new(f"w{wi}", "BOOLEAN"); mdw.operation = "DIFFERENCE"; mdw.solver = "EXACT"; mdw.object = win
        bpy.ops.object.modifier_apply(modifier=mdw.name); bpy.data.objects.remove(win, do_unlink=True)
    bp.data.materials.append(plate_mat); parts.append(bp)
    ybx = by + mm(2.0)
    for sgn in (1, -1):
        rxb = rounded_box("fe-rearx", mm(80.0), mm(3.0), mm(9.0), mm(2.0), seg=2)
        rxb.location = (cx, ybx, cz); rxb.rotation_euler = (0, math.radians(sgn * 33.0), 0)
        rxb.data.materials.append(xacc_mat); smooth(rxb, 30); parts.append(rxb)
    cart = box("fe-cartouche", mm(30.0), mm(1.2), mm(12.0), (cx, ybx + mm(0.4), cz))
    cart.data.materials.append(principled(f"fe-cart{idx}", (0.09, 0.093, 0.10), 0.42, metallic=0.85)); parts.append(cart)
    # bottom · PCIe bracket · G12 (GRADING-REPORT): SOLID dark-matte plate, NO vent grille (the FE
    # flow-through means no bracket perforations · HotHardware/HWCooling), 3x DisplayPort + 1x HDMI as
    # recessed cavities, plus the 2 mounting screws (HWCooling: "fixed with a pair of screws").
    bkz = cz - Hc / 2.0 - mm(7.0)
    brk = box("fe-bracket", Wc, mm(2.0), mm(16.0), (cx, yf + mm(4.0), bkz))
    brk.data.materials.append(brk_mat); parts.append(brk)
    # real FE I/O = 3x DisplayPort + 1x HDMI · recessed INTO the bracket plane (was floating proud)
    for j, (pw, ph) in enumerate([(mm(18.0), mm(8.0)), (mm(18.0), mm(8.0)), (mm(18.0), mm(8.0)), (mm(22.0), mm(6.5))]):
        io = box("fe-io", pw, mm(2.6), ph, (cx - mm(48.0) + j * mm(30.0), yf + mm(4.2), bkz))
        io.data.materials.append(dark); parts.append(io)
    # 2 bracket mounting screws at the ends (small recessed heads)
    for sx in (cx - Wc / 2.0 + mm(7.0), cx + Wc / 2.0 - mm(7.0)):
        bpy.ops.mesh.primitive_cylinder_add(radius=mm(2.4), depth=mm(2.0), vertices=16,
            location=(sx, yf + mm(3.4), bkz), rotation=(math.radians(90), 0, 0))
        sc = bpy.context.active_object; sc.name = "fe-brk-screw"
        sc.data.materials.append(plate_mat); smooth(sc, 30); parts.append(sc)
    # PCIe gold-finger contact edge · THE recognizable 'this is a real GPU' cue (panel-3 #5). A dark
    # PCB edge protruding below the shroud with a gold contact strip + the PCIe key notch.
    pcb = box(f"fe-pcb{idx}", mm(120.0), mm(1.7), mm(11.0), (cx, yc, cz - Hc / 2.0 - mm(13.0)))
    pcb.data.materials.append(principled(f"fe-pcbm{idx}", (0.016, 0.024, 0.018), 0.52, metallic=0.0)); parts.append(pcb)
    gold = principled(f"fe-gold{idx}", (0.63, 0.48, 0.17), 0.28, metallic=1.0)
    for gx, gw in ((cx - mm(34.0), mm(48.0)), (cx + mm(30.0), mm(56.0))):   # two finger banks split by the PCIe key notch
        gf = box("fe-goldfinger", gw, mm(1.9), mm(6.0), (gx, yc, cz - Hc / 2.0 - mm(17.0)))
        gf.data.materials.append(gold); parts.append(gf)
    # PCIe x16 SLOT connector · R2 (GRADING-REPORT): the card seats INTO a slot, not floating on bare
    # fingers. Two black rails straddle the PCB edge (the slot gap the gold fingers sit in) + end caps +
    # a retention latch at one end · then the riser board/adapter below it (the open-rig riser look).
    slot_mat = principled(f"fe-slot{idx}", (0.022, 0.022, 0.026), 0.5, metallic=0.2)
    slot_z = cz - Hc / 2.0 - mm(18.0)
    for sy in (yc - mm(1.7), yc + mm(1.7)):    # two rails straddling the ~1.7mm PCB (the slot gap)
        rail = box("fe-slot-rail", mm(92.0), mm(1.6), mm(9.0), (cx, sy, slot_z))
        rail.data.materials.append(slot_mat); parts.append(rail)
    for ex in (cx - mm(47.0), cx + mm(47.0)):  # connector end caps (close the slot ends)
        cap = box("fe-slot-cap", mm(4.0), mm(5.2), mm(9.0), (ex, yc, slot_z))
        cap.data.materials.append(slot_mat); parts.append(cap)
    latch = rounded_box("fe-slot-latch", mm(5.0), mm(5.0), mm(7.0), mm(1.0), seg=2)   # retention clip
    latch.location = (cx + mm(51.0), yc, slot_z - mm(1.0)); latch.data.materials.append(slot_mat)
    smooth(latch, 20); parts.append(latch)
    riser = box("fe-riser", mm(96.0), mm(20.0), mm(12.0), (cx, yc, cz - Hc / 2.0 - mm(28.0)))  # riser board/adapter below the slot
    riser.data.materials.append(principled(f"fe-riserc{idx}", (0.03, 0.03, 0.033), 0.5)); parts.append(riser)
    return parts

def _cable(name, pts, thick, mat):
    """A sleeved round cable through control points (NURBS + bevel). For the rig power leads (R1)."""
    cu = bpy.data.curves.new(name, "CURVE"); cu.dimensions = "3D"
    cu.bevel_depth = thick; cu.bevel_resolution = 4; cu.resolution_u = 10
    sp = cu.splines.new("NURBS"); sp.points.add(len(pts) - 1)
    for i, (x, y, z) in enumerate(pts):
        sp.points[i].co = (x, y, z, 1.0)
    sp.order_u = min(4, len(pts)); sp.use_endpoint_u = True
    ob = bpy.data.objects.new(name, cu); bpy.context.collection.objects.link(ob)
    ob.data.materials.append(mat)
    return ob

def build_gpu_row():
    """6 GPUs hung from a top mounting bar, fan-faces forward · a mobo tray + PSU at the base
    (the riser-mounted open-rig look). Owner spec: 'open row of 6 cards, fans out'."""
    W, H, D = mm(RACK["W"]), mm(RACK["H"]), mm(RACK["D"])
    fx, fy = W / 2.0, D / 2.0
    parts = []
    n = 6; pitch = mm(143.0)
    yc = -fy + mm(110.0)
    cz = mm(PLINTH) + mm(300.0)
    x0 = -pitch * (n - 1) / 2.0
    bar = box("gpu-mountbar", pitch * (n - 1) + mm(160), mm(30.0), mm(28.0),
              (0, yc + mm(6.0), cz + mm(304.0) / 2.0 + mm(20.0)))
    bar.data.materials.append(principled("gpu-bar", (0.055, 0.055, 0.062), 0.38, metallic=0.8)); parts.append(bar)
    # tiny per-card seat jitter (deterministic) · 6 hand-seated cards never sit in perfect co-planar
    # lockstep · vary the gap (dx) + vertical seat (dz) by <1mm to break the 'array of clones' read
    # (panel-3 #6). Pure translation (no rotation) · safe + clip-neutral.
    jit = [(0.6, -0.4), (-0.5, 0.5), (0.3, 0.2), (-0.4, -0.6), (0.5, 0.3), (-0.3, -0.2)]
    for i in range(n):
        dx, dz = jit[i]
        parts += build_gpu(x0 + i * pitch + mm(dx), cz + mm(dz), yc, idx=i)
    # base · a DARK mobo tray (was too bright, competed with the cards) carrying a populated
    # motherboard (dark PCB + CPU cooler + RAM + VRM heatsinks), a PSU, and a PCIe riser ribbon
    # from each card down to the board · fills the base + wires the rig (owner: 'real server rack').
    tray_z = mm(PLINTH) + mm(95.0)
    tray = box("mobo-tray", W - mm(160), D - mm(120), mm(4.0), (0, mm(10.0), tray_z))
    tray.data.materials.append(principled("mobo-tray-mat", (0.026, 0.026, 0.030), 0.60, metallic=0.25)); parts.append(tray)
    mobo = box("mobo", mm(305.0), mm(244.0), mm(3.0), (0, mm(42.0), tray_z + mm(3.5)))
    mobo.data.materials.append(principled("mobo-pcb", (0.020, 0.030, 0.022), 0.55)); parts.append(mobo)
    heat_mat = principled("mobo-heat", (0.10, 0.10, 0.11), 0.40, metallic=0.7)
    comp_mat = principled("mobo-comp", (0.05, 0.05, 0.055), 0.50, metallic=0.35)
    cpu = rounded_box("mobo-cpu", mm(92.0), mm(92.0), mm(30.0), mm(3.0), seg=3)
    cpu.location = (mm(18.0), mm(52.0), tray_z + mm(20.0)); cpu.data.materials.append(heat_mat); smooth(cpu, 30); parts.append(cpu)
    for i in range(4):
        ram = box("mobo-ram", mm(4.0), mm(122.0), mm(32.0), (mm(96.0) + i * mm(9.0), mm(52.0), tray_z + mm(20.0)))
        ram.data.materials.append(comp_mat); parts.append(ram)
    for vx in (-mm(96.0), -mm(52.0)):
        vrm = box("mobo-vrm", mm(52.0), mm(18.0), mm(16.0), (vx, mm(140.0), tray_z + mm(12.0)))
        vrm.data.materials.append(heat_mat); parts.append(vrm)
    psu = rounded_box("psu", mm(150.0), mm(86.0), mm(150.0), mm(3.0), seg=3)
    psu.location = (-fx + mm(150), mm(30.0), mm(PLINTH) + mm(63.0))
    psu.data.materials.append(principled("psu-mat", (0.028, 0.028, 0.032), 0.42, metallic=0.4)); smooth(psu, 30); parts.append(psu)
    rib_mat = principled("riser-ribbon", (0.035, 0.035, 0.040), 0.62, metallic=0.1)
    for i in range(n):
        rx = x0 + i * pitch
        rib = box("riser-ribbon", mm(56.0), mm(1.4), mm(150.0), (rx, yc + mm(58.0), cz - mm(190.0)))
        rib.rotation_euler = (math.radians(52.0), 0, 0)   # card bottom (front) -> mobo slot (rear, down)
        rib.data.materials.append(rib_mat); parts.append(rib)
    # POWER CABLES · R1 (GRADING-REPORT): six 575W cards MUST have visible 12V-2x6 power leads · a
    # cable-less rig reads as a mockup ("what powers these?"). One sleeved lead from each card's top-edge
    # connector (built in build_gpu at cx+68.5, top-rear), drooping back + down toward the PSU/tray.
    cab_mat = principled("gpu-pwr-cable", (0.020, 0.020, 0.023), 0.52, metallic=0.0)   # matte sleeved black
    Wc2 = mm(68.5)                       # card half-width (137/2) · the +X top edge with the connector
    conn_z = cz + mm(96.0)               # connector height (matches build_gpu pz = cz+96 on the +X edge)
    for i in range(n):
        dx, dz = jit[i]
        card_x = x0 + i * pitch + mm(dx)
        czc = cz + mm(dz)
        pts = [(card_x + Wc2 - mm(3), yc, czc + mm(96.0)),                 # at the recessed socket (+X top)
               (card_x + Wc2 + mm(9), yc + mm(12), czc + mm(103.0)),       # emerge +X, up + BACK (+Y)
               (card_x + Wc2 - mm(6), yc + mm(80), czc + mm(48.0)),        # arc BACK over the top-rear edge
               (card_x + mm(4), mm(64.0), tray_z + mm(150.0)),             # drape down toward the mobo (behind)
               (card_x - mm(10), mm(34.0), tray_z + mm(30.0))]             # into the PSU/tray zone at the base
        parts.append(_cable(f"gpu-cable{i}", pts, mm(3.4), cab_mat))
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
    add_area("key", (-0.8, -1.0, 1.15), 1.05, float(arg("--key", 96)), (1.0, 0.99, 0.97), aim=aim)  # softbox >= the ~0.96m rig · panel-4 #3: the rig-alone frames read underexposed vs the trio · key 72->96 for a brighter, graded read
    add_area("rim", (0.6, 0.95, 1.1), 0.06, float(arg("--rim", 60)), (0.93, 0.96, 1.0), sx=0.9, aim=aim)
    add_area("fill", (0.1, -1.2, 0.55), 1.1, float(arg("--fill", 22)), (0.97, 0.98, 1.0), aim=aim)
    bpy.ops.mesh.primitive_plane_add(size=8.0, location=(0, 0, 0))
    fl = bpy.context.active_object; fl.name = "floor"
    # floor · a hair reflective so the rig GROUNDS with a faint reflection (the #1 panel tell was
    # 'it floats') · stays near-black so the premium void look holds · the reflection reads the weight.
    fl.data.materials.append(floor_mat())  # grounds via a soft smudge-varied reflection (panel-4 #4 · not a perfect mirror)
    return aim

def rack_camera(aim, shot, res):
    sc = bpy.context.scene
    cd = bpy.data.cameras.new("cam"); cd.lens = 70.0; cd.sensor_width = 36.0
    cam = bpy.data.objects.new("cam", cd); bpy.context.collection.objects.link(cam); sc.camera = cam
    H = mm(RACK["H"])
    dist = 2.5    # wide (~0.86m) 6-GPU rig · was 4.6 for the 2m cabinet
    yaw, elev = {"front": (0.0, 4.0), "frame-front": (0.0, 4.0), "q34": (32.0, 8.0),
                 "side": (90.0, 6.0), "rear": (180.0, 4.0), "rearq34": (212.0, 9.0),
                 "top": (18.0, 55.0)}.get(shot, (32.0, 8.0))
    ya, el = math.radians(yaw), math.radians(elev)
    ax, ay, az = aim.location
    cam.location = (ax + dist * math.cos(el) * math.sin(ya),
                    ay - dist * math.cos(el) * math.cos(ya),
                    az + dist * math.sin(el))
    c = cam.constraints.new("TRACK_TO"); c.target = aim
    c.track_axis = "TRACK_NEGATIVE_Z"; c.up_axis = "UP_Y"
    cd.dof.use_dof = True; cd.dof.focus_object = aim; cd.dof.aperture_fstop = 16.0
    sc.render.resolution_x, sc.render.resolution_y = res

def setup_post(sc):
    """Hero post-chain (compositor) · a subtle FOG-GLOW bloom so the lit inlet rings + X read as
    real LEDs with a soft halo, plus a gentle vignette. Photographic finish · never on gate frames."""
    sc.use_nodes = True
    nt = sc.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    rl = nt.nodes.new("CompositorNodeRLayers")
    glare = nt.nodes.new("CompositorNodeGlare")
    glare.glare_type = "FOG_GLOW"; glare.quality = "HIGH"; glare.threshold = 0.82; glare.size = 6
    try: glare.mix = -0.55   # keep the bloom subtle (blend toward the original)
    except Exception: pass
    lens = nt.nodes.new("CompositorNodeLensdist")
    try: lens.inputs["Dispersion"].default_value = 0.004   # a hair of chromatic aberration at the edge
    except Exception: pass
    # subtle vignette · real lenses darken the frame corners (was promised, never built)
    mask = nt.nodes.new("CompositorNodeEllipseMask"); mask.width = 1.15; mask.height = 1.15
    vblur = nt.nodes.new("CompositorNodeBlur"); vblur.filter_type = "FAST_GAUSS"; vblur.size_x = 240; vblur.size_y = 240
    vmap = nt.nodes.new("CompositorNodeMapRange")
    vmap.inputs["To Min"].default_value = 0.80; vmap.inputs["To Max"].default_value = 1.0
    vig = nt.nodes.new("CompositorNodeMixRGB"); vig.blend_type = "MULTIPLY"; vig.inputs["Fac"].default_value = 1.0
    comp = nt.nodes.new("CompositorNodeComposite")
    nt.links.new(rl.outputs["Image"], glare.inputs["Image"])
    nt.links.new(glare.outputs["Image"], lens.inputs["Image"])
    nt.links.new(mask.outputs["Mask"], vblur.inputs["Image"])
    nt.links.new(vblur.outputs["Image"], vmap.inputs["Value"])
    nt.links.new(lens.outputs["Image"], vig.inputs[1])
    nt.links.new(vmap.outputs["Value"], vig.inputs[2])
    nt.links.new(vig.outputs["Image"], comp.inputs["Image"])

def render_to(path):
    sc = bpy.context.scene; sc.render.filepath = path
    if POST:
        setup_post(sc)
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
if PART == "defs":
    pass   # exec-only: load the builders (no scene, no render) · used by build_trio.py
elif PART == "node":
    build_rm44_node()
    node_rig_camera(SHOT, (1800, 1400))
    render_to(OUT + f"node-{SHOT}.png")
    print("build_rack RM44 node proof done.")
elif PART == "switch":
    build_crs354_switch()
    switch_rig_camera(SHOT, (1900, 900))
    render_to(OUT + f"switch-{SHOT}.png")
    print("build_rack CRS354 switch proof done.")
elif PART == "gpu":
    # ONE RTX 5090 FE · 360 audit rig (front/q34/rear/rearq34/top/side/bottom · SHOT selects).
    build_gpu(0.0, 0.0, 0.0, idx=0)
    aim = bpy.data.objects.new("Aim", None); aim.location = (0, 0, 0)
    bpy.context.collection.objects.link(aim)
    add_area("key", (-0.55, -0.7, 0.6), 0.4, float(arg("--key", 42)), (1.0, 0.99, 0.97), aim=aim)
    add_area("rim", (0.5, 0.6, 0.55), 0.04, float(arg("--rim", 30)), (0.93, 0.96, 1.0), sx=0.5, aim=aim)
    add_area("fill", (0.1, -0.75, 0.2), 0.7, float(arg("--fill", 12)), (0.97, 0.98, 1.0), aim=aim)
    # G17 · bottom-shot relight: the underside (bracket/fingers/riser) sits near the floor · at elev -60
    # the camera dropped UNDER the floor plane, which then occluded everything (a solid-black frame). For
    # the bottom shot, skip the floor and add an under-fill so the PCIe end reads.
    if SHOT == "bottom":
        add_area("underfill", (0.0, -0.5, -0.8), 0.6, 15.0, (1.0, 0.99, 0.97), aim=aim)
    else:
        bpy.ops.mesh.primitive_plane_add(size=4.0, location=(0, 0, -0.17))
        _fl = bpy.context.active_object; _fl.data.materials.append(principled("floor", (0.006, 0.006, 0.007), 0.62))
    _sc = bpy.context.scene
    _cd = bpy.data.cameras.new("cam"); _cd.lens = 85.0; _cd.sensor_width = 36.0
    _cam = bpy.data.objects.new("cam", _cd); bpy.context.collection.objects.link(_cam); _sc.camera = _cam
    _dist = 0.30 if SHOT == "macro" else 0.66
    _shots = {"front": (0, 3), "q34": (32, 10), "rear": (180, 3), "rearq34": (212, 12),
              "top": (0, 72), "side": (90, 5), "bottom": (22, -42), "macro": (18, 7)}
    if SHOT == "macro":
        aim.location = (0.0, 0.0, mm(34.0))   # feature-aimed between the X accent + the top fan
    if SHOT == "bottom":
        aim.location = (0.0, 0.0, -mm(150.0))   # aim at the PCIe bracket / gold-finger end
    _yaw, _elev = _shots.get(SHOT, (32, 10))
    _ya, _el = math.radians(_yaw), math.radians(_elev)
    ax, ay, az = aim.location
    _cam.location = (ax + _dist * math.cos(_el) * math.sin(_ya), ay - _dist * math.cos(_el) * math.cos(_ya), az + _dist * math.sin(_el))
    _c = _cam.constraints.new("TRACK_TO"); _c.target = aim; _c.track_axis = "TRACK_NEGATIVE_Z"; _c.up_axis = "UP_Y"
    _cd.dof.use_dof = True; _cd.dof.focus_object = aim; _cd.dof.aperture_fstop = 2.8 if SHOT == "macro" else 8.0
    _sc.render.resolution_x, _sc.render.resolution_y = (1500, 1800)
    render_to(OUT + f"gpu-{SHOT}.png")
    print("build_rack single 5090 FE done.")
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
