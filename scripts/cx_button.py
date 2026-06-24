# computexchange action buttons: the SAME oracle as the power knob, ELONGATED.
# Run: /Applications/Blender.app/Contents/MacOS/Blender -b -P scripts/cx_button.py
# Mirrors cx_knob.py exactly — dark anodized aluminium, beveled rim, cool-white
# emissive accent, same lighting + Glare bloom — but the disc becomes an elongated
# slab with a recessed center where the LABEL sits (instead of the power glyph) and a
# glowing perimeter frame. Three states like the knob: off / armed / pressed.
import bpy, bmesh, math, os
OUT = "/Users/scammermike/Downloads/computexchange/web/assets/"
os.makedirs(OUT, exist_ok=True)

bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene; sc.render.engine = 'CYCLES'
try:
    cp = bpy.context.preferences.addons['cycles'].preferences
    for dt in ('METAL', 'OPTIX', 'CUDA', 'HIP'):
        try: cp.compute_device_type = dt; break
        except TypeError: continue
    cp.get_devices()
    for d in cp.devices: d.use = True
    sc.cycles.device = 'GPU'
except Exception as e: print("CPU fallback:", e)
sc.cycles.samples = 512; sc.cycles.use_denoising = True
try: sc.cycles.denoiser = 'OPENIMAGEDENOISE'
except Exception: pass
sc.render.film_transparent = True
try: sc.view_settings.view_transform = 'AgX'; sc.view_settings.look = 'AgX - High Contrast'
except Exception: sc.view_settings.view_transform = 'Filmic'
sc.view_settings.exposure = -0.6
sc.render.image_settings.file_format = 'PNG'; sc.render.image_settings.color_mode = 'RGBA'
sc.render.resolution_x = 1100; sc.render.resolution_y = 300   # elongated; CSS stretches it

# ── elongated slab body (the knob's profile, stretched) ──
bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 0))
slab = bpy.context.active_object; slab.name = "Button"
slab.scale = (3.0, 0.82, 0.17)   # 6.0 x 1.64 x 0.34 (same 0.34 height as the knob)
bpy.ops.object.transform_apply(scale=True)
bev = slab.modifiers.new("Bevel", 'BEVEL'); bev.width = 0.07; bev.segments = 6
bev.limit_method = 'ANGLE'; bev.angle_limit = math.radians(40)
bpy.ops.object.shade_smooth()
# recessed center (where the label sits)
bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 0.20))
cut = bpy.context.active_object; cut.scale = (2.78, 0.62, 0.08); bpy.ops.object.transform_apply(scale=True)
b = slab.modifiers.new("Recess", 'BOOLEAN'); b.operation = 'DIFFERENCE'; b.object = cut
bpy.context.view_layer.objects.active = slab; bpy.ops.object.modifier_apply(modifier="Recess")
bpy.data.objects.remove(cut, do_unlink=True)

# ── knob material (identical: dark anodized, turned, coat) ──
def metal(name, knurl=False):
    m = bpy.data.materials.new(name); m.use_nodes = True
    nt = m.node_tree; bsdf = nt.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (0.07, 0.07, 0.085, 1)
    bsdf.inputs["Metallic"].default_value = 1.0
    bsdf.inputs["Roughness"].default_value = 0.24
    bsdf.inputs["Anisotropic"].default_value = 0.35   # softened so the wide face does not swirl
    try:
        bsdf.inputs["Coat Weight"].default_value = 0.5; bsdf.inputs["Coat Roughness"].default_value = 0.12
    except Exception:
        bsdf.inputs["Clearcoat"].default_value = 0.5
    tan = nt.nodes.new("ShaderNodeTangent"); tan.direction_type = 'RADIAL'; tan.axis = 'Z'
    nt.links.new(tan.outputs["Tangent"], bsdf.inputs["Tangent"])
    if knurl:
        wave = nt.nodes.new("ShaderNodeTexWave"); wave.wave_type = 'BANDS'; wave.inputs["Scale"].default_value = 90
        bump = nt.nodes.new("ShaderNodeBump"); bump.inputs["Strength"].default_value = 0.5
        nt.links.new(wave.outputs["Fac"], bump.inputs["Height"]); nt.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
    return m
face_mat = metal("Face"); side_mat = metal("Side", knurl=True)
slab.data.materials.append(face_mat); slab.data.materials.append(side_mat)
me = slab.data; bm = bmesh.new(); bm.from_mesh(me)
for f in bm.faces:
    f.material_index = 1 if abs(f.normal.z) < 0.45 else 0   # knurl the near-vertical rim
bm.to_mesh(me); bm.free()

# ── emissive perimeter frame (the knob's glowing ring, elongated) ──
emis = bpy.data.materials.new("Emis"); emis.use_nodes = True
ent = emis.node_tree
for n in list(ent.nodes):
    if n.type != 'OUTPUT_MATERIAL': ent.nodes.remove(n)
em = ent.nodes.new("ShaderNodeEmission")
em.inputs["Color"].default_value = (0.86, 0.92, 1.0, 1); em.inputs["Strength"].default_value = 0.0
ent.links.new(em.outputs["Emission"], ent.nodes["Material Output"].inputs["Surface"])
bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 0.135)); outer = bpy.context.active_object
outer.scale = (2.62, 0.52, 0.006); bpy.ops.object.transform_apply(scale=True)
bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 0.135)); inner = bpy.context.active_object
inner.scale = (2.48, 0.40, 0.03); bpy.ops.object.transform_apply(scale=True)
fb = outer.modifiers.new("Hole", 'BOOLEAN'); fb.operation = 'DIFFERENCE'; fb.object = inner
bpy.context.view_layer.objects.active = outer; bpy.ops.object.modifier_apply(modifier="Hole")
bpy.data.objects.remove(inner, do_unlink=True)
fbev = outer.modifiers.new("FB", 'BEVEL'); fbev.width = 0.015; fbev.segments = 3
outer.data.materials.append(emis); bpy.ops.object.shade_smooth()

# ── lighting / world / shadow / camera / bloom (identical rig to the knob) ──
target = bpy.data.objects.new("Aim", None); target.location = (0, 0, 0.1); bpy.context.collection.objects.link(target)
def aim(o):
    c = o.constraints.new('TRACK_TO'); c.target = target; c.track_axis = 'TRACK_NEGATIVE_Z'; c.up_axis = 'UP_Y'
def area(name, loc, size, energy, color=(1, 1, 1), sx=None):
    l = bpy.data.lights.new(name, 'AREA'); l.energy = energy; l.color = color
    if sx: l.shape = 'RECTANGLE'; l.size = sx; l.size_y = size
    else: l.size = size
    o = bpy.data.objects.new(name, l); o.location = loc; bpy.context.collection.objects.link(o); return o
for o in [area("Key", (-3.0, -2.4, 3.6), 3.4, 420, (0.96, 0.98, 1.0)),
          area("Fill", (3.0, -1.0, 1.8), 2.4, 70),
          area("Strip", (-2.0, -2.4, 2.8), 5.0, 360, sx=0.10),
          area("Rim", (0.6, 2.4, 2.4), 2.0, 110)]:
    aim(o)
world = bpy.data.worlds.new("W"); world.use_nodes = True
world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.008, 0.008, 0.01, 1); sc.world = world
bpy.ops.mesh.primitive_plane_add(size=14, location=(0, 0, -0.18)); plane = bpy.context.active_object
try: plane.is_shadow_catcher = True
except Exception: pass
cam_d = bpy.data.cameras.new("Cam"); cam_d.type = 'ORTHO'; cam_d.ortho_scale = 6.6
cam = bpy.data.objects.new("Cam", cam_d); cam.location = (0, -2.4, 1.9)
bpy.context.collection.objects.link(cam); aim(cam); sc.camera = cam
sc.use_nodes = True; ct = sc.node_tree
for n in list(ct.nodes): ct.nodes.remove(n)
rl = ct.nodes.new("CompositorNodeRLayers"); glare = ct.nodes.new("CompositorNodeGlare")
glare.glare_type = 'FOG_GLOW'; glare.threshold = 0.6; glare.size = 7
comp = ct.nodes.new("CompositorNodeComposite")
ct.links.new(rl.outputs["Image"], glare.inputs["Image"]); ct.links.new(glare.outputs["Image"], comp.inputs["Image"])

def render(p): sc.render.filepath = p; bpy.ops.render.render(write_still=True)
em.inputs["Strength"].default_value = 0.0; render(OUT + "button-off@3x.png")     # disabled (dark groove)
em.inputs["Strength"].default_value = 9.0; render(OUT + "button-armed@3x.png")    # armed (glowing frame)
slab.location.z -= 0.02; outer.location.z -= 0.02; render(OUT + "button-press@3x.png")  # pressed (pushed down)
print("done ->", OUT)
