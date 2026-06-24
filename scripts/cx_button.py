# computexchange action buttons (Launch, Add payment): Blender (Cycles) scaffold.
# A SIBLING of cx_knob.py: same dark anodized metal, lighting, and Glare bloom — the
# difference is the SHAPE (a rounded key, not a stretched disc) and the LIT element
# (the engraved, backlit LABEL, not a power glyph). No brushed-steel variant, no
# stretched-radial smear. Renders off / armed / pressed for LAUNCH and ADD PAYMENT.
# Run: /Applications/Blender.app/Contents/MacOS/Blender -b -P scripts/cx_button.py
import bpy, os

OUT = "/Users/scammermike/Downloads/computexchange/web/assets/"
os.makedirs(OUT, exist_ok=True)


def engine():
    sc = bpy.context.scene
    sc.render.engine = 'CYCLES'
    try:
        cp = bpy.context.preferences.addons['cycles'].preferences
        for dt in ('METAL', 'OPTIX', 'CUDA', 'HIP'):
            try:
                cp.compute_device_type = dt
                break
            except TypeError:
                continue
        cp.get_devices()
        for d in cp.devices:
            d.use = True
        sc.cycles.device = 'GPU'
    except Exception as e:
        print("CPU fallback:", e)
    sc.cycles.samples = 512
    sc.cycles.use_denoising = True
    try:
        sc.cycles.denoiser = 'OPENIMAGEDENOISE'
    except Exception:
        pass
    sc.render.film_transparent = True
    try:
        sc.view_settings.view_transform = 'AgX'
        sc.view_settings.look = 'AgX - High Contrast'
    except Exception:
        sc.view_settings.view_transform = 'Filmic'
    sc.view_settings.exposure = -0.78   # match the knob's dark anodized read (it was too bright/chrome)
    sc.render.image_settings.file_format = 'PNG'
    sc.render.image_settings.color_mode = 'RGBA'
    sc.render.resolution_x = 1200
    sc.render.resolution_y = 360


def build_button(label, W, H, T, CORNER, textsize, slug, resx, resy):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    engine()
    sc = bpy.context.scene
    sc.render.resolution_x = resx; sc.render.resolution_y = resy   # match the button's aspect

    # rounded-key slab: a box, beveled to round the corners + rim.
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 0))
    slab = bpy.context.active_object
    slab.scale = (W / 2, H / 2, T / 2)
    bpy.ops.object.transform_apply(scale=True)
    bev = slab.modifiers.new("Bevel", 'BEVEL')
    bev.width = CORNER; bev.segments = 10; bev.limit_method = 'NONE'; bev.use_clamp_overlap = True
    bpy.ops.object.shade_smooth()
    bpy.ops.object.modifier_apply(modifier="Bevel")

    # anodized metal (the knob's language: dark, coated, a crisp rim specular).
    m = bpy.data.materials.new("ButtonMetal"); m.use_nodes = True
    nt = m.node_tree; bsdf = nt.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (0.075, 0.075, 0.09, 1)
    bsdf.inputs["Metallic"].default_value = 1.0
    bsdf.inputs["Roughness"].default_value = 0.52   # diffuse the flat-top mirror -> dark anodized, not chrome
    try:
        bsdf.inputs["Coat Weight"].default_value = 0.5; bsdf.inputs["Coat Roughness"].default_value = 0.12
    except Exception:
        bsdf.inputs["Clearcoat"].default_value = 0.5
    # fine horizontal brushed grain (stretched noise: no radial pinch, no banding)
    tc = nt.nodes.new("ShaderNodeTexCoord")
    mp = nt.nodes.new("ShaderNodeMapping"); mp.inputs["Scale"].default_value = (6.0, 700.0, 1.0)
    nz = nt.nodes.new("ShaderNodeTexNoise"); nz.inputs["Scale"].default_value = 1.0; nz.inputs["Detail"].default_value = 2.0
    bump = nt.nodes.new("ShaderNodeBump"); bump.inputs["Strength"].default_value = 0.07
    nt.links.new(tc.outputs["Object"], mp.inputs["Vector"])
    nt.links.new(mp.outputs["Vector"], nz.inputs["Vector"])
    nt.links.new(nz.outputs["Fac"], bump.inputs["Height"])
    nt.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
    slab.data.materials.append(m)

    # engraved, backlit label — the lit element.
    bpy.ops.object.text_add(location=(0, 0, T / 2 - 0.012))
    txt = bpy.context.active_object
    txt.data.body = label
    txt.data.align_x = 'CENTER'; txt.data.align_y = 'CENTER'
    txt.data.size = textsize
    txt.data.extrude = 0.012
    txt.data.space_character = 1.15
    emis = bpy.data.materials.new("LabelEmis"); emis.use_nodes = True
    ent = emis.node_tree
    for n in list(ent.nodes):
        if n.type != 'OUTPUT_MATERIAL':
            ent.nodes.remove(n)
    em = ent.nodes.new("ShaderNodeEmission")
    em.inputs["Color"].default_value = (0.91, 0.95, 1.0, 1)   # cool white
    em.inputs["Strength"].default_value = 0.0                 # state variable
    ent.links.new(em.outputs["Emission"], ent.nodes["Material Output"].inputs["Surface"])
    txt.data.materials.append(emis)

    # lighting (mirror the knob).
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
    area("Strip", (-1.8, -2.4, 2.6), 3.0, 110, sx=0.12)
    area("Rim", (0.6, 2.2, 2.2), 1.4, 60)
    world = bpy.data.worlds.new("W"); world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.008, 0.008, 0.01, 1); sc.world = world

    bpy.ops.mesh.primitive_plane_add(size=12, location=(0, 0, -T / 2 - 0.001))
    try:
        bpy.context.active_object.is_shadow_catcher = True
    except Exception:
        pass

    cam_d = bpy.data.cameras.new("Cam"); cam_d.type = 'ORTHO'; cam_d.ortho_scale = W * 1.12
    cam = bpy.data.objects.new("Cam", cam_d); cam.location = (0, -1.9, 2.4)   # more top-down (like the knob): top face reflects the dark world
    bpy.context.collection.objects.link(cam)
    cc = cam.constraints.new('TRACK_TO'); cc.target = target; cc.track_axis = 'TRACK_NEGATIVE_Z'; cc.up_axis = 'UP_Y'
    sc.camera = cam

    sc.use_nodes = True; ct = sc.node_tree
    for n in list(ct.nodes):
        ct.nodes.remove(n)
    rl = ct.nodes.new("CompositorNodeRLayers")
    gl = ct.nodes.new("CompositorNodeGlare"); gl.glare_type = 'FOG_GLOW'; gl.threshold = 0.9; gl.size = 6
    co = ct.nodes.new("CompositorNodeComposite")
    ct.links.new(rl.outputs["Image"], gl.inputs["Image"]); ct.links.new(gl.outputs["Image"], co.inputs["Image"])

    def render(p):
        sc.render.filepath = p; bpy.ops.render.render(write_still=True)
    em.inputs["Strength"].default_value = 0.0; render(OUT + "btn-" + slug + "-off@3x.png")      # disabled (dark engraving)
    em.inputs["Strength"].default_value = 2.8; render(OUT + "btn-" + slug + "-armed@3x.png")    # armed (glowing label)
    slab.location.z -= 0.015; txt.location.z -= 0.015
    render(OUT + "btn-" + slug + "-pressed@3x.png")                                              # pressed (pushed in)


# Wide Launch control: a wide rounded-rectangle, smaller radius, rendered at the
# full-width button aspect (~7.5:1). Add payment: a full pill at its own aspect.
build_button("LAUNCH", 6.0, 0.80, 0.20, 0.20, 0.30, "launch", 1500, 200)
build_button("ADD PAYMENT", 2.6, 0.78, 0.20, 0.39, 0.20, "add-payment", 1080, 360)
print("done ->", OUT)
