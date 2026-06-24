# computexchange run-status oracle: the metal RING only -- a mini sibling of the knob, a
# bold turned-metal annulus on a transparent ground. The neon core (green = done/running,
# red = failed) is drawn in CSS inside the ring, so it stays a vivid, app-matched colour
# (AgX desaturates a bright emissive toward white, the same reason the button labels are
# CSS too). One colour-agnostic ring serves both states.
# Run: /Applications/Blender.app/Contents/MacOS/Blender -b -P scripts/cx_status.py
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
sc.cycles.samples = 640
sc.cycles.use_denoising = True
sc.render.film_transparent = True
try:
    sc.view_settings.view_transform = 'AgX'
    sc.view_settings.look = 'AgX - High Contrast'
except Exception:
    sc.view_settings.view_transform = 'Filmic'
sc.view_settings.exposure = -0.5
sc.render.image_settings.file_format = 'PNG'
sc.render.image_settings.color_mode = 'RGBA'
sc.render.resolution_x = 320
sc.render.resolution_y = 320

# outer metal ring: a disc with the centre bored out -> a bold annulus (transparent hole).
bpy.ops.mesh.primitive_cylinder_add(vertices=192, radius=1.0, depth=0.34, location=(0, 0, 0))
ring = bpy.context.active_object
bpy.ops.mesh.primitive_cylinder_add(vertices=160, radius=0.62, depth=0.6, location=(0, 0, 0))
bore = bpy.context.active_object
b = ring.modifiers.new("Bore", 'BOOLEAN'); b.operation = 'DIFFERENCE'; b.object = bore
bpy.context.view_layer.objects.active = ring
bpy.ops.object.modifier_apply(modifier="Bore")
bpy.data.objects.remove(bore, do_unlink=True)
bev = ring.modifiers.new("Bevel", 'BEVEL'); bev.width = 0.045; bev.segments = 6
bev.limit_method = 'ANGLE'; bev.angle_limit = math.radians(40)
bpy.ops.object.shade_smooth()
bpy.ops.object.modifier_apply(modifier="Bevel")

# turned anodized metal (the knob's recipe: radial anisotropy).
m = bpy.data.materials.new("RingMetal"); m.use_nodes = True
nt = m.node_tree; bsdf = nt.nodes["Principled BSDF"]
bsdf.inputs["Base Color"].default_value = (0.07, 0.07, 0.085, 1)
bsdf.inputs["Metallic"].default_value = 1.0
bsdf.inputs["Roughness"].default_value = 0.26
bsdf.inputs["Anisotropic"].default_value = 0.85
try:
    bsdf.inputs["Coat Weight"].default_value = 0.5; bsdf.inputs["Coat Roughness"].default_value = 0.12
except Exception:
    bsdf.inputs["Clearcoat"].default_value = 0.5
tan = nt.nodes.new("ShaderNodeTangent"); tan.direction_type = 'RADIAL'; tan.axis = 'Z'
nt.links.new(tan.outputs["Tangent"], bsdf.inputs["Tangent"])
ring.data.materials.append(m)

# lighting (the knob rig, oriented to the +Z-facing ring).
target = bpy.data.objects.new("Aim", None); target.location = (0, 0, 0.05); bpy.context.collection.objects.link(target)


def area(name, loc, size, energy, color=(1, 1, 1), sx=None):
    l = bpy.data.lights.new(name, 'AREA'); l.energy = energy; l.color = color
    if sx:
        l.shape = 'RECTANGLE'; l.size = sx; l.size_y = size
    else:
        l.size = size
    o = bpy.data.objects.new(name, l); o.location = loc; bpy.context.collection.objects.link(o)
    c = o.constraints.new('TRACK_TO'); c.target = target; c.track_axis = 'TRACK_NEGATIVE_Z'; c.up_axis = 'UP_Y'


area("Key", (-2.6, 2.4, 4.2), 3.0, 340, (0.96, 0.98, 1.0))
area("Fill", (2.8, -0.8, 3.4), 2.2, 90)
area("Strip", (-2.2, -2.6, 3.4), 3.0, 170, sx=0.12)
area("Rim", (1.6, 2.6, 2.4), 1.4, 80)
world = bpy.data.worlds.new("W"); world.use_nodes = True
world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.008, 0.008, 0.01, 1); sc.world = world

cam_d = bpy.data.cameras.new("Cam"); cam_d.type = 'ORTHO'; cam_d.ortho_scale = 2.24
cam = bpy.data.objects.new("Cam", cam_d); cam.location = (0, 0, 6.0)
bpy.context.collection.objects.link(cam)
cc = cam.constraints.new('TRACK_TO'); cc.target = target; cc.track_axis = 'TRACK_NEGATIVE_Z'; cc.up_axis = 'UP_Y'
sc.camera = cam

# mild bloom for the metal specular only (no emissive here).
sc.use_nodes = True; ct = sc.node_tree
for n in list(ct.nodes):
    ct.nodes.remove(n)
rl = ct.nodes.new("CompositorNodeRLayers")
gl = ct.nodes.new("CompositorNodeGlare"); gl.glare_type = 'FOG_GLOW'; gl.threshold = 1.0; gl.size = 5
co = ct.nodes.new("CompositorNodeComposite")
ct.links.new(rl.outputs["Image"], gl.inputs["Image"]); ct.links.new(gl.outputs["Image"], co.inputs["Image"])

sc.render.filepath = OUT + "dot-ring@3x.png"
bpy.ops.render.render(write_still=True)
print("done ->", OUT)
