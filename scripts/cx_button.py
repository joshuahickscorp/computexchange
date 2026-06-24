# computexchange action buttons (Launch, Add payment, Run pipeline): Blender (Cycles).
# A PILL: a rounded-rectangle slab in the knob's dark anodized metal, with a shallow recess
# cut into the face and a backlit LABEL engraved into it. Viewed top-down so it reads as a
# flat, compact pill (not a tilted disc). The metal is LINEAR brushed (a flat rectangle has
# no radial convergence to hide, so the turned/radial finish of the round knob is replaced
# by a straight brush). The label uses the SYSTEM font (San Francisco), NEVER Geist.
# Engine look + exposure match cx_knob.py so the metal reads from the same family.
# Run: /Applications/Blender.app/Contents/MacOS/Blender -b -P scripts/cx_button.py
import bpy, os

OUT = "/Users/scammermike/Downloads/computexchange/web/assets/"
os.makedirs(OUT, exist_ok=True)

SYS_FONT_CANDIDATES = [
    "/System/Library/Fonts/SFNS.ttf",
    "/System/Library/Fonts/SFNSDisplay.ttf",
    "/System/Library/Fonts/SFCompact.ttf",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
]


def metal():
    # dark anodized, LINEAR brushed (no radial convergence on a flat pill), with a coat for
    # the crisp rim specular. Same base colour + metallic + coat as the knob.
    m = bpy.data.materials.new("BtnMetal"); m.use_nodes = True
    nt = m.node_tree; bsdf = nt.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (0.075, 0.075, 0.09, 1)
    bsdf.inputs["Metallic"].default_value = 1.0
    bsdf.inputs["Roughness"].default_value = 0.40
    try:
        bsdf.inputs["Coat Weight"].default_value = 0.5; bsdf.inputs["Coat Roughness"].default_value = 0.12
    except Exception:
        bsdf.inputs["Clearcoat"].default_value = 0.5
    tc = nt.nodes.new("ShaderNodeTexCoord")
    mp = nt.nodes.new("ShaderNodeMapping"); mp.inputs["Scale"].default_value = (5.0, 600.0, 1.0)
    nz = nt.nodes.new("ShaderNodeTexNoise"); nz.inputs["Scale"].default_value = 1.0; nz.inputs["Detail"].default_value = 2.0
    bump = nt.nodes.new("ShaderNodeBump"); bump.inputs["Strength"].default_value = 0.06
    nt.links.new(tc.outputs["Object"], mp.inputs["Vector"])
    nt.links.new(mp.outputs["Vector"], nz.inputs["Vector"])
    nt.links.new(nz.outputs["Fac"], bump.inputs["Height"])
    nt.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
    return m


def rounded_box(W, H, T, CORNER, z, name):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, z))
    ob = bpy.context.active_object; ob.name = name
    ob.scale = (W / 2, H / 2, T / 2)
    bpy.context.view_layer.objects.active = ob
    bpy.ops.object.transform_apply(scale=True)
    bev = ob.modifiers.new("Bevel", 'BEVEL'); bev.width = CORNER; bev.segments = 12
    bev.limit_method = 'NONE'; bev.use_clamp_overlap = True
    bpy.ops.object.shade_smooth()
    bpy.ops.object.modifier_apply(modifier="Bevel")
    return ob


def build(LABEL, W, H, T, CORNER, LABEL_SIZE, slug):
    # ---------- clean scene + engine (the knob's look: Metal GPU, AgX High Contrast, -0.6) ----------
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = 'CYCLES'
    try:
        cp = bpy.context.preferences.addons['cycles'].preferences
        for dt in ('METAL', 'OPTIX', 'CUDA', 'HIP'):
            try:
                cp.compute_device_type = dt; break
            except TypeError:
                continue
        cp.get_devices()
        for d in cp.devices:
            d.use = True
        sc.cycles.device = 'GPU'
    except Exception as e:
        print("CPU fallback:", e)
    sc.cycles.samples = 1024
    sc.cycles.use_denoising = True
    sc.render.film_transparent = True
    try:
        sc.view_settings.view_transform = 'AgX'
        sc.view_settings.look = 'AgX - High Contrast'
    except Exception:
        sc.view_settings.view_transform = 'Filmic'
    sc.view_settings.exposure = -0.6
    sc.render.image_settings.file_format = 'PNG'
    sc.render.image_settings.color_mode = 'RGBA'
    sc.render.resolution_x = 1600
    sc.render.resolution_y = max(360, int(1600 / (W / H) * 1.5))

    # ---------- body: the rounded-rectangle pill ----------
    slab = rounded_box(W, H, T, CORNER, 0.0, "Button")
    slab.data.materials.append(metal())

    # ---------- a shallow recess cut into the face (the label sits in it) ----------
    cut = rounded_box(W - 1.4, H - 0.42, 0.10, 0.12, T / 2, "Cut")  # spans the top ~0.05 of the face
    b = slab.modifiers.new("Recess", 'BOOLEAN'); b.operation = 'DIFFERENCE'; b.object = cut
    bpy.context.view_layer.objects.active = slab
    bpy.ops.object.modifier_apply(modifier="Recess")
    bpy.data.objects.remove(cut, do_unlink=True)

    # ---------- emissive label (cool white; an action, not status), engraved in the recess ----------
    emis = bpy.data.materials.new("BtnEmis"); emis.use_nodes = True
    ent = emis.node_tree
    for n in list(ent.nodes):
        if n.type != 'OUTPUT_MATERIAL':
            ent.nodes.remove(n)
    em = ent.nodes.new("ShaderNodeEmission")
    em.inputs["Color"].default_value = (0.91, 0.95, 1.0, 1)
    em.inputs["Strength"].default_value = 0.0
    ent.links.new(em.outputs["Emission"], ent.nodes["Material Output"].inputs["Surface"])

    bpy.ops.object.text_add(location=(0, 0, T / 2 - 0.045))
    txt = bpy.context.active_object
    txt.data.body = LABEL
    txt.data.align_x = 'CENTER'; txt.data.align_y = 'CENTER'
    txt.data.size = LABEL_SIZE
    txt.data.extrude = 0.006
    _font = None
    for fp in SYS_FONT_CANDIDATES:
        if os.path.exists(fp):
            try:
                _font = bpy.data.fonts.load(fp); break
            except Exception:
                pass
    if _font is not None:
        txt.data.font = _font
    txt.data.materials.append(emis)
    txt.parent = slab

    # ---------- lighting (key, fill, strip, rim) ----------
    target = bpy.data.objects.new("Aim", None); target.location = (0, 0, 0.05); bpy.context.collection.objects.link(target)

    def area(name, loc, size, energy, color=(1, 1, 1), sx=None):
        l = bpy.data.lights.new(name, 'AREA'); l.energy = energy; l.color = color
        if sx:
            l.shape = 'RECTANGLE'; l.size = sx; l.size_y = size
        else:
            l.size = size
        o = bpy.data.objects.new(name, l); o.location = loc; bpy.context.collection.objects.link(o)
        c = o.constraints.new('TRACK_TO'); c.target = target; c.track_axis = 'TRACK_NEGATIVE_Z'; c.up_axis = 'UP_Y'

    area("Key", (-2.4, -2.2, 3.4), 3.0, 230, (0.96, 0.98, 1.0))
    area("Fill", (2.6, -1.0, 1.6), 2.2, 60)
    area("Strip", (-1.8, -2.4, 2.6), 3.0, 120, sx=0.12)
    area("Rim", (0.6, 2.2, 2.2), 1.4, 60)
    world = bpy.data.worlds.new("W"); world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.008, 0.008, 0.01, 1)
    sc.world = world

    bpy.ops.mesh.primitive_plane_add(size=8 * max(1.0, W * 0.4), location=(0, 0, -T / 2 - 0.001))
    try:
        bpy.context.active_object.is_shadow_catcher = True
    except Exception:
        pass

    # ---------- camera (orthographic, slightly top-down; the pill reads flat + compact) ----------
    cam_d = bpy.data.cameras.new("Cam"); cam_d.type = 'ORTHO'; cam_d.ortho_scale = W * 1.06
    cam = bpy.data.objects.new("Cam", cam_d); cam.location = (0, -1.7, 2.7)   # mostly top-down
    bpy.context.collection.objects.link(cam)
    cc = cam.constraints.new('TRACK_TO'); cc.target = target; cc.track_axis = 'TRACK_NEGATIVE_Z'; cc.up_axis = 'UP_Y'
    sc.camera = cam

    # ---------- compositor bloom (Glare, Fog Glow) ----------
    sc.use_nodes = True; ct = sc.node_tree
    for n in list(ct.nodes):
        ct.nodes.remove(n)
    rl = ct.nodes.new("CompositorNodeRLayers")
    glare = ct.nodes.new("CompositorNodeGlare"); glare.glare_type = 'FOG_GLOW'; glare.threshold = 0.6; glare.size = 7
    comp = ct.nodes.new("CompositorNodeComposite")
    ct.links.new(rl.outputs["Image"], glare.inputs["Image"])
    ct.links.new(glare.outputs["Image"], comp.inputs["Image"])

    def render(p):
        sc.render.filepath = p; bpy.ops.render.render(write_still=True)
    em.inputs["Strength"].default_value = 0.0; render(OUT + "btn-" + slug + "-off@3x.png")      # disabled (dark engraving)
    em.inputs["Strength"].default_value = 6.0; render(OUT + "btn-" + slug + "-armed@3x.png")     # armed (backlit, breathes in CSS)
    slab.location.z -= 0.02; render(OUT + "btn-" + slug + "-pressed@3x.png")                     # push
    print("done ->", slug)


build("LAUNCH", 6.2, 1.0, 0.18, 0.42, 0.50, "launch")
build("ADD PAYMENT", 3.4, 1.0, 0.18, 0.42, 0.40, "add-payment")
print("done ->", OUT)
