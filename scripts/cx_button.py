import bpy, bmesh, os

OUT = "web/assets/"
os.makedirs(OUT, exist_ok=True)

# slug, L (overall length), H (height), T (thickness). Chunky so the round ends read as a pill.
BUTTONS = [
    # same H + same CSS height (see demo.html) -> matched bevel finish. L and the end-bezel
    # coefficient `bez` differ: a smaller pill needs a smaller bez so the bezel doesn't read chunky.
    ("launch",      2.7, 0.92, 0.34, 1.15),   # text "Launch"
    ("add-payment", 1.6, 0.92, 0.34, 0.62),   # compact icon pill: thinner end bezel to match Launch's proportion
]

def make_capsule(L, H, T):
    """True capsule (stadium prism): a straight middle box plus two half-cylinder end caps.
    The ends are real semicircles of radius H/2, so this can never be a rounded rectangle."""
    mid = max(0.0001, L - H)                  # length of the straight middle section
    # size=2.0 -> cube half-extent 1.0, so scaling by half-dimensions gives full (mid, H, T):
    # box height must equal the cap diameter H, else the middle reads as a thin neck (dumbbell).
    bpy.ops.mesh.primitive_cube_add(size=2.0, location=(0, 0, 0))
    body = bpy.context.active_object
    body.scale = (mid/2, H/2, T/2)
    bpy.ops.object.transform_apply(scale=True)
    for sx in (-1, 1):
        bpy.ops.mesh.primitive_cylinder_add(vertices=96, radius=H/2, depth=T,
                                             location=(sx*mid/2, 0, 0))
        cap = bpy.context.active_object
        bpy.context.view_layer.objects.active = body
        m = body.modifiers.new("Union", 'BOOLEAN'); m.operation = 'UNION'
        m.solver = 'EXACT'; m.object = cap
        bpy.ops.object.modifier_apply(modifier="Union")
        bpy.data.objects.remove(cap, do_unlink=True)
    return body

def render_button(slug, L, H, T, bez=1.15):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = 'CYCLES'
    try:
        cp = bpy.context.preferences.addons['cycles'].preferences
        cp.compute_device_type = 'METAL'      # M series; or 'OPTIX' / 'CUDA' / 'HIP'
        cp.get_devices_for_type('METAL')      # populate cp.devices (required in headless 4.x)
        for d in cp.devices: d.use = True
        sc.cycles.device = 'GPU'
    except Exception:
        pass
    sc.cycles.samples = 1024
    sc.cycles.use_denoising = True
    sc.render.film_transparent = True
    try:
        sc.view_settings.view_transform = 'AgX'
        sc.view_settings.look = 'AgX - High Contrast'   # match the knob's punchier tone
    except Exception:
        sc.view_settings.view_transform = 'Filmic'
    sc.view_settings.exposure = -0.30   # darker graphite body; only the strip/rim highlights stay bright
    sc.render.image_settings.file_format = 'PNG'
    sc.render.image_settings.color_mode = 'RGBA'
    sc.render.resolution_x = 1600
    # tight, even framing: pill nearly fills the frame so it fills the button (no wasted
    # vertical space). res_y = 1600*H/L gives equal padding on all sides via ortho_scale below.
    sc.render.resolution_y = max(280, int(1600 * H / L))

    # ---- body: a TRUE capsule ----
    pill = make_capsule(L, H, T); pill.name = "Pill"
    bpy.context.view_layer.objects.active = pill
    # soften only the sharp rim edges with a small bevel; too small to change the outline
    rb = pill.modifiers.new("Rim", 'BEVEL')
    rb.width = 0.09; rb.segments = 8; rb.limit_method = 'ANGLE'; rb.angle_limit = 0.6   # rounder rim -> brighter machined-edge highlight
    bpy.ops.object.modifier_apply(modifier="Rim")
    bpy.ops.object.shade_smooth()

    # ---- recessed channel: a smaller capsule cut into the top ----
    rh = H * 0.60                              # channel height: leaves a thick steel bezel top/bottom
    rl = L - H * bez                           # channel length: smaller bez -> thinner end bezel
    cutter = make_capsule(rl, rh, 0.16); cutter.name = "Cut"
    cutter.location = (0, 0, T/2)             # cuts about 0.08 down into the top face
    bpy.context.view_layer.objects.active = pill
    db = pill.modifiers.new("Recess", 'BOOLEAN'); db.operation = 'DIFFERENCE'
    db.solver = 'EXACT'; db.object = cutter
    bpy.ops.object.modifier_apply(modifier="Recess")
    bpy.data.objects.remove(cutter, do_unlink=True)

    # ---- materials: light steel face and rim, dark recess floor so the white label reads ----
    def mk(name, base, rough, metal=1.0, coat=0.5, coatr=0.10, aniso=0.0, brushed=False):
        m = bpy.data.materials.new(name); m.use_nodes = True
        nt = m.node_tree; b = nt.nodes["Principled BSDF"]
        b.inputs["Base Color"].default_value = (base[0], base[1], base[2], 1)
        b.inputs["Metallic"].default_value = metal
        b.inputs["Roughness"].default_value = rough
        try: b.inputs["Anisotropic"].default_value = aniso
        except Exception: pass
        try:
            b.inputs["Coat Weight"].default_value = coat
            b.inputs["Coat Roughness"].default_value = coatr
        except Exception:
            b.inputs["Clearcoat"].default_value = coat
            b.inputs["Clearcoat Roughness"].default_value = coatr
        if aniso > 0:                                  # turned-metal sheen, same recipe as the knob
            tan = nt.nodes.new("ShaderNodeTangent"); tan.direction_type = 'RADIAL'; tan.axis = 'Z'
            nt.links.new(tan.outputs["Tangent"], b.inputs["Tangent"])
        if brushed:
            tc = nt.nodes.new("ShaderNodeTexCoord")
            mp = nt.nodes.new("ShaderNodeMapping"); mp.inputs["Scale"].default_value = (6.0, 700.0, 1.0)
            nz = nt.nodes.new("ShaderNodeTexNoise"); nz.inputs["Scale"].default_value = 1.0; nz.inputs["Detail"].default_value = 2.0
            bump = nt.nodes.new("ShaderNodeBump"); bump.inputs["Strength"].default_value = 0.03
            nt.links.new(tc.outputs["Object"], mp.inputs["Vector"])
            nt.links.new(mp.outputs["Vector"], nz.inputs["Vector"])
            nt.links.new(nz.outputs["Fac"], bump.inputs["Height"])
            nt.links.new(bump.outputs["Normal"], b.inputs["Normal"])
        return m

    # dark gunmetal + turned anisotropy + clearcoat: the knob's machined-steel recipe. A metal's
    # body IS its reflections, so a dark base over a near-black world stays near-black except where
    # the strip/key lights streak bright across it -> that black-to-bright range is the sheen.
    # Rim is a touch brighter so the bevel pops as a bright machined edge.
    face_mat  = mk("Face",  (0.10,0.11,0.13), 0.22, coat=0.6, coatr=0.10, aniso=0.9)   # smooth turned, no linear brush
    rim_mat   = mk("Rim",   (0.18,0.19,0.22), 0.08, coat=0.6, coatr=0.06, aniso=0.9)
    floor_mat = mk("Floor", (0.012,0.012,0.016), 0.92, metal=0.10, coat=0.0)
    pill.data.materials.append(face_mat)   # 0
    pill.data.materials.append(rim_mat)    # 1
    pill.data.materials.append(floor_mat)  # 2

    me = pill.data; bm = bmesh.new(); bm.from_mesh(me)
    top_z = max(v.co.z for v in bm.verts)
    for f in bm.faces:
        nz = f.normal.z
        cz = sum(v.co.z for v in f.verts) / len(f.verts)
        if nz > 0.9 and cz < top_z - 0.02:
            f.material_index = 2          # recess floor (dark matte)
        elif nz > 0.92:
            f.material_index = 0          # top face (light satin)
        elif nz > 0.25:
            f.material_index = 1          # rim (polished light)
        else:
            f.material_index = 0
    bm.to_mesh(me); bm.free()

    # ---- world + lights: this is what made them LIGHT. Keep it. ----
    world = bpy.data.worlds.new("W"); world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.01,0.01,0.013,1)  # near-black world (knob value): max sheen contrast
    sc.world = world
    target = bpy.data.objects.new("Aim", None); target.location=(0,0,0.0); bpy.context.collection.objects.link(target)
    def aim(o):
        c = o.constraints.new('TRACK_TO'); c.target = target; c.track_axis = 'TRACK_NEGATIVE_Z'; c.up_axis = 'UP_Y'
    def area(name, loc, size, energy, color=(1,1,1), sx=None):
        l = bpy.data.lights.new(name, 'AREA'); l.energy = energy; l.color = color
        if sx: l.shape = 'RECTANGLE'; l.size = sx; l.size_y = size
        else: l.size = size
        o = bpy.data.objects.new(name, l); o.location = loc; bpy.context.collection.objects.link(o); return o
    sp = max(2.0, L * 0.45)
    # knob lighting: no flat overhead flood (that washes the metal to flat gray). A directional key
    # softbox + a thin bright STRIP for the crisp product-render streak + a rim edge, near-black world.
    for o in [area("Soft",  (0.4,  0.2, 4.0), max(5.0, L*1.4), 35, (0.85,0.9,1.0)),       # faint lift so the bezel isn't dead black
              area("Key",   (-sp, -2.2, 3.2), 2.6, 140, (0.96,0.98,1.0)),                 # main softbox streak
              area("Strip", (-sp*0.3, -1.0, 3.6), L*1.05, 450, (1.0,1.0,1.0), sx=0.08),   # crisp strip-light highlight (the soul)
              area("Rim",   (0.5,  2.4, 2.4), 1.6, 150)]:                                 # bright machined edge
        aim(o)

    # No shadow catcher: against the dark world the lit ground plane reads as a bright halo
    # around the pill. The UI adds a clean pill-shaped shadow via a CSS drop-shadow filter.

    # ---- camera: straight overhead, PERPENDICULAR, no tilt. A flat button facing the viewer. ----
    cam_d = bpy.data.cameras.new("Cam"); cam_d.type = 'ORTHO'; cam_d.ortho_scale = L * 1.12
    cam = bpy.data.objects.new("Cam", cam_d); cam.location = (0, 0, 3.2)
    bpy.context.collection.objects.link(cam); aim(cam); sc.camera = cam

    # compositor bloom: Fog Glow on the bright streaks/rim -> the knob's "rendered glow". Safe now
    # the metal is dark: only the highlights exceed threshold and bloom, not the whole surface.
    sc.use_nodes = True; ct = sc.node_tree
    for n in list(ct.nodes): ct.nodes.remove(n)
    crl = ct.nodes.new("CompositorNodeRLayers")
    cg = ct.nodes.new("CompositorNodeGlare"); cg.glare_type = 'FOG_GLOW'; cg.threshold = 0.6; cg.size = 7
    cc = ct.nodes.new("CompositorNodeComposite")
    ct.links.new(crl.outputs["Image"], cg.inputs["Image"])
    ct.links.new(cg.outputs["Image"], cc.inputs["Image"])

    sc.render.filepath = OUT + "btn-" + slug + "-shell@3x.png"
    bpy.ops.render.render(write_still=True)
    print("done ->", OUT + "btn-" + slug + "-shell@3x.png")

for slug, L, H, T, bez in BUTTONS:
    render_button(slug, L, H, T, bez)
