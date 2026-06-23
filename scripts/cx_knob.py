# computexchange power knob: Blender (Cycles) build + render.
# Run headless: /Applications/Blender.app/Contents/MacOS/Blender -b -P scripts/cx_knob.py
# Targets Blender 4.2 (Metal GPU on Apple Silicon). Renders the three state PNGs
# (off / on / pressed) the UI composites in Appendix B. Static render is the
# deliverable per the doctrine's anti-loop rule.
import bpy, bmesh, math, os

OUT = "/Users/scammermike/Downloads/computexchange/web/assets/"
os.makedirs(OUT, exist_ok=True)

# ---------- clean scene + engine ----------
bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
sc.render.engine = 'CYCLES'
# Metal GPU on Apple Silicon; fall back across backends, then CPU.
try:
    cp = bpy.context.preferences.addons['cycles'].preferences
    for dt in ('METAL', 'OPTIX', 'CUDA', 'HIP', 'ONEAPI'):
        try:
            cp.compute_device_type = dt
            break
        except TypeError:
            continue
    cp.get_devices()
    for d in cp.devices:
        d.use = True
    sc.cycles.device = 'GPU'
    print("cycles backend:", cp.compute_device_type, "devices:", [d.name for d in cp.devices])
except Exception as e:
    print("GPU setup failed, using CPU:", e)

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
sc.view_settings.exposure = -0.6   # deepen the body so it reads as dark anodized metal, not light plastic
sc.render.image_settings.file_format = 'PNG'
sc.render.image_settings.color_mode = 'RGBA'
sc.render.resolution_x = 720
sc.render.resolution_y = 720

# ---------- body ----------
bpy.ops.mesh.primitive_cylinder_add(vertices=256, radius=1.0, depth=0.34, location=(0, 0, 0))
puck = bpy.context.active_object
puck.name = "Knob"
bev = puck.modifiers.new("Bevel", 'BEVEL')
bev.width = 0.03; bev.segments = 5; bev.limit_method = 'ANGLE'; bev.angle_limit = math.radians(40)
bpy.ops.object.shade_smooth()

# shallow center recess via boolean
bpy.ops.mesh.primitive_cylinder_add(vertices=128, radius=0.45, depth=0.06, location=(0, 0, 0.165))
cut = bpy.context.active_object
b = puck.modifiers.new("Recess", 'BOOLEAN'); b.operation = 'DIFFERENCE'; b.object = cut
bpy.context.view_layer.objects.active = puck
bpy.ops.object.modifier_apply(modifier="Recess")
bpy.data.objects.remove(cut, do_unlink=True)

# ---------- materials ----------
def metal(name, knurl=False):
    m = bpy.data.materials.new(name); m.use_nodes = True
    nt = m.node_tree; bsdf = nt.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (0.07, 0.07, 0.085, 1)
    bsdf.inputs["Metallic"].default_value = 1.0
    bsdf.inputs["Roughness"].default_value = 0.24   # tighter -> crisp specular streaks, dark body between
    bsdf.inputs["Anisotropic"].default_value = 0.9
    try:
        bsdf.inputs["Coat Weight"].default_value = 0.5
        bsdf.inputs["Coat Roughness"].default_value = 0.12
    except Exception:
        bsdf.inputs["Clearcoat"].default_value = 0.5
        bsdf.inputs["Clearcoat Roughness"].default_value = 0.12
    # turned (concentric) anisotropy via radial tangent
    tan = nt.nodes.new("ShaderNodeTangent"); tan.direction_type = 'RADIAL'; tan.axis = 'Z'
    nt.links.new(tan.outputs["Tangent"], bsdf.inputs["Tangent"])
    if knurl:
        wave = nt.nodes.new("ShaderNodeTexWave"); wave.wave_type = 'BANDS'; wave.bands_direction = 'X'; wave.inputs["Scale"].default_value = 90
        bump = nt.nodes.new("ShaderNodeBump"); bump.inputs["Strength"].default_value = 0.55
        nt.links.new(wave.outputs["Fac"], bump.inputs["Height"])
        nt.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
    return m

face_mat = metal("KnobFace", knurl=False)
side_mat = metal("KnobSide", knurl=True)
puck.data.materials.append(face_mat)   # index 0
puck.data.materials.append(side_mat)   # index 1

# assign side material to near-horizontal-normal faces (the rim)
me = puck.data; bm = bmesh.new(); bm.from_mesh(me)
for f in bm.faces:
    f.material_index = 1 if abs(f.normal.z) < 0.45 else 0
bm.to_mesh(me); bm.free()

# ---------- emissive glyph ----------
emis = bpy.data.materials.new("KnobEmis"); emis.use_nodes = True
ent = emis.node_tree
for n in list(ent.nodes):
    if n.type != 'OUTPUT_MATERIAL':
        ent.nodes.remove(n)
em = ent.nodes.new("ShaderNodeEmission")
em.inputs["Color"].default_value = (0.86, 0.92, 1.0, 1)
em.inputs["Strength"].default_value = 0.0   # state variable
ent.links.new(em.outputs["Emission"], ent.nodes["Material Output"].inputs["Surface"])

def add_emissive(obj):
    obj.data.materials.append(emis); bpy.ops.object.shade_smooth()

# power circle (near-closed) + bar + perimeter ring, all in the recess
bpy.ops.mesh.primitive_torus_add(location=(0, 0, 0.145), major_radius=0.16, minor_radius=0.025)
add_emissive(bpy.context.active_object)
bpy.ops.mesh.primitive_cylinder_add(vertices=48, radius=0.025, depth=0.18, location=(0, 0, 0.18))
add_emissive(bpy.context.active_object)
bpy.ops.mesh.primitive_torus_add(location=(0, 0, 0.155), major_radius=0.82, minor_radius=0.02)
add_emissive(bpy.context.active_object)

# ---------- lighting (key, fill, strip, rim) ----------
target = bpy.data.objects.new("Aim", None); target.location = (0, 0, 0.1)
bpy.context.collection.objects.link(target)

def aim(o):
    cns = o.constraints.new('TRACK_TO'); cns.target = target
    cns.track_axis = 'TRACK_NEGATIVE_Z'; cns.up_axis = 'UP_Y'

def area(name, loc, size, energy, color=(1, 1, 1), sx=None):
    l = bpy.data.lights.new(name, 'AREA'); l.energy = energy; l.color = color
    if sx:
        l.shape = 'RECTANGLE'; l.size = sx; l.size_y = size
    else:
        l.size = size
    o = bpy.data.objects.new(name, l); o.location = loc
    bpy.context.collection.objects.link(o)
    return o

for o in [area("Key", (-2.2, -2.2, 3.4), 2.6, 170, (0.96, 0.98, 1.0)),
          area("Fill", (2.6, -1.0, 1.6), 2.0, 42),
          area("Strip", (-1.6, -2.2, 2.6), 2.6, 240, sx=0.10),
          area("Rim", (0.5, 2.2, 2.2), 1.4, 75)]:
    aim(o)

world = bpy.data.worlds.new("W"); world.use_nodes = True
world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.008, 0.008, 0.01, 1)
sc.world = world

# ---------- shadow catcher ----------
bpy.ops.mesh.primitive_plane_add(size=8, location=(0, 0, -0.17))
plane = bpy.context.active_object
try:
    plane.is_shadow_catcher = True
except Exception:
    pass

# ---------- camera (orthographic, slight top-down) ----------
cam_d = bpy.data.cameras.new("Cam"); cam_d.type = 'ORTHO'; cam_d.ortho_scale = 2.45
cam = bpy.data.objects.new("Cam", cam_d); cam.location = (0, -2.6, 1.9)
bpy.context.collection.objects.link(cam); aim(cam); sc.camera = cam

# ---------- compositor bloom (Glare, Fog Glow) ----------
sc.use_nodes = True; ct = sc.node_tree
for n in list(ct.nodes):
    ct.nodes.remove(n)
rl = ct.nodes.new("CompositorNodeRLayers")
glare = ct.nodes.new("CompositorNodeGlare"); glare.glare_type = 'FOG_GLOW'; glare.threshold = 0.6; glare.size = 7
comp = ct.nodes.new("CompositorNodeComposite")
ct.links.new(rl.outputs["Image"], glare.inputs["Image"])
ct.links.new(glare.outputs["Image"], comp.inputs["Image"])

# ---------- render the states ----------
def render(path):
    sc.render.filepath = path; bpy.ops.render.render(write_still=True)

em.inputs["Strength"].default_value = 0.0; render(OUT + "knob-off@3x.png")
em.inputs["Strength"].default_value = 9.0; render(OUT + "knob-on@3x.png")
puck.location.z -= 0.02; render(OUT + "knob-pressed@3x.png")
print("done ->", OUT)
