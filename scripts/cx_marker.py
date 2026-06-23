# computexchange status markers: small metal pucks that echo the power knob.
# Run: /Applications/Blender.app/Contents/MacOS/Blender -b -P scripts/cx_marker.py
# Renders three state markers (running glow / done inert / failed dark) at small size,
# sharing the knob's metal, lighting, and bloom so they read as a family. State must
# read at ~16px, so the geometry is simpler than the knob (no power glyph / knurl).
import bpy, math, os

OUT = "/Users/scammermike/Downloads/computexchange/web/assets/"
os.makedirs(OUT, exist_ok=True)

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
sc.cycles.samples = 384
sc.cycles.use_denoising = True
try:
    sc.cycles.denoiser = 'OPENIMAGEDENOISE'
except Exception:
    pass
sc.render.film_transparent = True
try:
    sc.view_settings.view_transform = 'AgX'; sc.view_settings.look = 'AgX - High Contrast'
except Exception:
    sc.view_settings.view_transform = 'Filmic'
sc.view_settings.exposure = -0.5
sc.render.image_settings.file_format = 'PNG'; sc.render.image_settings.color_mode = 'RGBA'
sc.render.resolution_x = 200; sc.render.resolution_y = 200

# ---------- puck body (shared) ----------
bpy.ops.mesh.primitive_cylinder_add(vertices=192, radius=1.0, depth=0.32, location=(0, 0, 0))
puck = bpy.context.active_object; puck.name = "Marker"
bev = puck.modifiers.new("Bevel", 'BEVEL'); bev.width = 0.05; bev.segments = 5
bev.limit_method = 'ANGLE'; bev.angle_limit = math.radians(40)
bpy.ops.object.shade_smooth()
bpy.ops.mesh.primitive_cylinder_add(vertices=96, radius=0.5, depth=0.08, location=(0, 0, 0.15))
cut = bpy.context.active_object
b = puck.modifiers.new("Recess", 'BOOLEAN'); b.operation = 'DIFFERENCE'; b.object = cut
bpy.context.view_layer.objects.active = puck
bpy.ops.object.modifier_apply(modifier="Recess")
bpy.data.objects.remove(cut, do_unlink=True)

def metal():
    m = bpy.data.materials.new("M"); m.use_nodes = True
    bsdf = m.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (0.07, 0.07, 0.085, 1)
    bsdf.inputs["Metallic"].default_value = 1.0
    bsdf.inputs["Roughness"].default_value = 0.24
    bsdf.inputs["Anisotropic"].default_value = 0.9
    try:
        bsdf.inputs["Coat Weight"].default_value = 0.5; bsdf.inputs["Coat Roughness"].default_value = 0.12
    except Exception:
        bsdf.inputs["Clearcoat"].default_value = 0.5
    tan = m.node_tree.nodes.new("ShaderNodeTangent"); tan.direction_type = 'RADIAL'; tan.axis = 'Z'
    m.node_tree.links.new(tan.outputs["Tangent"], bsdf.inputs["Tangent"])
    return m
puck.data.materials.append(metal())

def emis_mat(strength):
    m = bpy.data.materials.new("E"); m.use_nodes = True
    nt = m.node_tree
    for n in list(nt.nodes):
        if n.type != 'OUTPUT_MATERIAL':
            nt.nodes.remove(n)
    e = nt.nodes.new("ShaderNodeEmission")
    e.inputs["Color"].default_value = (0.86, 0.92, 1.0, 1); e.inputs["Strength"].default_value = strength
    nt.links.new(e.outputs["Emission"], nt.nodes["Material Output"].inputs["Surface"])
    return m

def dark_mat():
    m = bpy.data.materials.new("D"); m.use_nodes = True
    bsdf = m.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (0.015, 0.015, 0.02, 1)
    bsdf.inputs["Metallic"].default_value = 0.2; bsdf.inputs["Roughness"].default_value = 0.6
    return m

# ---------- lighting (mirror the knob) ----------
target = bpy.data.objects.new("Aim", None); target.location = (0, 0, 0.1); bpy.context.collection.objects.link(target)
def aim(o):
    c = o.constraints.new('TRACK_TO'); c.target = target; c.track_axis = 'TRACK_NEGATIVE_Z'; c.up_axis = 'UP_Y'
def area(name, loc, size, energy, color=(1, 1, 1), sx=None):
    l = bpy.data.lights.new(name, 'AREA'); l.energy = energy; l.color = color
    if sx:
        l.shape = 'RECTANGLE'; l.size = sx; l.size_y = size
    else:
        l.size = size
    o = bpy.data.objects.new(name, l); o.location = loc; bpy.context.collection.objects.link(o); return o
for o in [area("Key", (-2.2, -2.2, 3.4), 2.6, 170, (0.96, 0.98, 1.0)),
          area("Fill", (2.6, -1.0, 1.6), 2.0, 42),
          area("Strip", (-1.6, -2.2, 2.6), 2.6, 240, sx=0.10),
          area("Rim", (0.5, 2.2, 2.2), 1.4, 75)]:
    aim(o)
world = bpy.data.worlds.new("W"); world.use_nodes = True
world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.008, 0.008, 0.01, 1); sc.world = world
bpy.ops.mesh.primitive_plane_add(size=8, location=(0, 0, -0.16)); plane = bpy.context.active_object
try:
    plane.is_shadow_catcher = True
except Exception:
    pass
cam_d = bpy.data.cameras.new("Cam"); cam_d.type = 'ORTHO'; cam_d.ortho_scale = 2.3
cam = bpy.data.objects.new("Cam", cam_d); cam.location = (0, -2.6, 1.9)
bpy.context.collection.objects.link(cam); aim(cam); sc.camera = cam
sc.use_nodes = True; ct = sc.node_tree
for n in list(ct.nodes):
    ct.nodes.remove(n)
rl = ct.nodes.new("CompositorNodeRLayers")
glare = ct.nodes.new("CompositorNodeGlare"); glare.glare_type = 'FOG_GLOW'; glare.threshold = 0.6; glare.size = 6
comp = ct.nodes.new("CompositorNodeComposite")
ct.links.new(rl.outputs["Image"], glare.inputs["Image"]); ct.links.new(glare.outputs["Image"], comp.inputs["Image"])

# ---------- per-state center feature ----------
center = []
def clear_center():
    global center
    for o in center:
        bpy.data.objects.remove(o, do_unlink=True)
    center = []
def boss(mat):           # flat disc filling the recess (used for the dark failed center)
    bpy.ops.mesh.primitive_cylinder_add(vertices=96, radius=0.46, depth=0.06, location=(0, 0, 0.14))
    o = bpy.context.active_object; o.data.materials.append(mat); bpy.ops.object.shade_smooth(); center.append(o); return o
def dome(mat):           # shallow polished dome: a convex center catches a bright specular hotspot
    bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, radius=0.44, location=(0, 0, 0.02))
    o = bpy.context.active_object; o.scale = (1, 1, 0.42)
    bpy.ops.object.transform_apply(scale=True)
    o.data.materials.append(mat); bpy.ops.object.shade_smooth(); center.append(o); return o
def ring(mat):           # emissive ring in the recess
    bpy.ops.mesh.primitive_torus_add(location=(0, 0, 0.13), major_radius=0.32, minor_radius=0.06)
    o = bpy.context.active_object; o.data.materials.append(mat); bpy.ops.object.shade_smooth(); center.append(o); return o

def render(path):
    sc.render.filepath = path; bpy.ops.render.render(write_still=True)

# done: a polished metal dome (bright specular hotspot, settled and complete)
clear_center(); dome(metal()); render(OUT + "dot-done@3x.png")
# running: a glowing cool-white ring + bloom (alive)
clear_center(); ring(emis_mat(9.0)); render(OUT + "dot-on@3x.png")
# failed: a dark recessed center, no light (inert, clearly wrong, no red)
clear_center(); boss(dark_mat()); render(OUT + "dot-fail@3x.png")
print("done ->", OUT)
