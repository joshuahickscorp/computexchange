# computexchange splash mark: the metal Cx. A SIBLING of cx_knob.py / cx_button.py.
# The C is the machined, anodized BODY (the flat-letterform metal recipe from cx_button:
# linear brushing, no radial pinch, so a wide letter does not smear). The x is the
# engraved, backlit EMISSIVE glyph sitting in the C's counter, the same relationship the
# power glyph has to the knob face. Same lights + Glare bloom as the knob.
# Geometry comes from scripts/cx_trace.py (run that first -> /tmp/cx_paths.json).
# Run: /Applications/Blender.app/Contents/MacOS/Blender -b -P scripts/cx_logo.py
import bpy, bmesh, json, os

OUT = "/Users/scammermike/Downloads/computexchange/logo/"
PATHS = "/tmp/cx_paths.json"
os.makedirs(OUT, exist_ok=True)
paths = json.load(open(PATHS))

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
sc.cycles.samples = 768
sc.cycles.use_denoising = True
sc.render.film_transparent = True
try:
    sc.view_settings.view_transform = 'AgX'
    sc.view_settings.look = 'AgX - High Contrast'
except Exception:
    sc.view_settings.view_transform = 'Filmic'
sc.view_settings.exposure = -0.55
sc.render.image_settings.file_format = 'PNG'
sc.render.image_settings.color_mode = 'RGBA'
sc.render.resolution_x = 1080
sc.render.resolution_y = 1080


# ---------- geometry from traced contours ----------
def build(name, pts, thickness, bevel):
    bm = bmesh.new()
    vs = [bm.verts.new((p[0], p[1], 0.0)) for p in pts]
    bm.faces.new(vs)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    me = bpy.data.meshes.new(name); bm.to_mesh(me); bm.free()
    ob = bpy.data.objects.new(name, me); bpy.context.collection.objects.link(ob)
    sol = ob.modifiers.new("Solidify", 'SOLIDIFY'); sol.thickness = thickness; sol.offset = 0.0
    bev = ob.modifiers.new("Bevel", 'BEVEL'); bev.width = bevel; bev.segments = 6
    bev.limit_method = 'ANGLE'; bev.angle_limit = 1.05; bev.use_clamp_overlap = True
    bpy.context.view_layer.objects.active = ob
    bpy.ops.object.modifier_apply(modifier="Solidify")
    bpy.ops.object.modifier_apply(modifier="Bevel")
    bpy.ops.object.shade_smooth()
    return ob

C = build("C", paths["C"], thickness=0.20, bevel=0.022)
X = build("x", paths["x"], thickness=0.10, bevel=0.012)
X.location.z = 0.02   # nudge the glyph forward in the counter so its glow catches

# ---------- materials ----------
# C: the cx_button flat-letterform metal (linear brushed grain, no radial pinch).
m = bpy.data.materials.new("CxMetal"); m.use_nodes = True
nt = m.node_tree; bsdf = nt.nodes["Principled BSDF"]
bsdf.inputs["Base Color"].default_value = (0.075, 0.075, 0.09, 1)
bsdf.inputs["Metallic"].default_value = 1.0
bsdf.inputs["Roughness"].default_value = 0.30
try:
    bsdf.inputs["Coat Weight"].default_value = 0.5; bsdf.inputs["Coat Roughness"].default_value = 0.12
except Exception:
    bsdf.inputs["Clearcoat"].default_value = 0.5
tc = nt.nodes.new("ShaderNodeTexCoord")
mp = nt.nodes.new("ShaderNodeMapping"); mp.inputs["Scale"].default_value = (6.0, 700.0, 1.0)
nz = nt.nodes.new("ShaderNodeTexNoise"); nz.inputs["Scale"].default_value = 1.0; nz.inputs["Detail"].default_value = 2.0
bump = nt.nodes.new("ShaderNodeBump"); bump.inputs["Strength"].default_value = 0.07
nt.links.new(tc.outputs["Object"], mp.inputs["Vector"])
nt.links.new(mp.outputs["Vector"], nz.inputs["Vector"])
nt.links.new(nz.outputs["Fac"], bump.inputs["Height"])
nt.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
C.data.materials.append(m)

# x: emissive backlit glyph (the knob's emission). Strength toggled per frame.
emis = bpy.data.materials.new("CxGlyph"); emis.use_nodes = True
ent = emis.node_tree
for n in list(ent.nodes):
    if n.type != 'OUTPUT_MATERIAL':
        ent.nodes.remove(n)
em = ent.nodes.new("ShaderNodeEmission")
em.inputs["Color"].default_value = (0.90, 0.94, 1.0, 1)   # cool white
em.inputs["Strength"].default_value = 0.0
ent.links.new(em.outputs["Emission"], ent.nodes["Material Output"].inputs["Surface"])
X.data.materials.append(emis)

# ---------- lighting (the knob/button rig, oriented to the +Z-facing mark) ----------
target = bpy.data.objects.new("Aim", None); target.location = (0, 0, 0.1)
bpy.context.collection.objects.link(target)


def area(name, loc, size, energy, color=(1, 1, 1), sx=None):
    l = bpy.data.lights.new(name, 'AREA'); l.energy = energy; l.color = color
    if sx:
        l.shape = 'RECTANGLE'; l.size = sx; l.size_y = size
    else:
        l.size = size
    o = bpy.data.objects.new(name, l); o.location = loc; bpy.context.collection.objects.link(o)
    c = o.constraints.new('TRACK_TO'); c.target = target; c.track_axis = 'TRACK_NEGATIVE_Z'; c.up_axis = 'UP_Y'


area("Key", (-2.6, 2.4, 4.2), 3.2, 360, (0.96, 0.98, 1.0))
area("Fill", (2.8, -0.8, 3.4), 2.4, 90)
area("Strip", (-2.2, -2.6, 3.4), 3.2, 170, sx=0.12)   # thin -> a crisp specular sweep line
area("Rim", (1.6, 2.6, 2.4), 1.6, 70)
world = bpy.data.worlds.new("W"); world.use_nodes = True
world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.008, 0.008, 0.01, 1)
sc.world = world

# ---------- camera (orthographic, straight-on; frame to ~10% padding like cx-mark-white) ----------
cam_d = bpy.data.cameras.new("Cam"); cam_d.type = 'ORTHO'; cam_d.ortho_scale = 2.42
cam = bpy.data.objects.new("Cam", cam_d); cam.location = (0, 0, 6.0)
bpy.context.collection.objects.link(cam)
cc = cam.constraints.new('TRACK_TO'); cc.target = target; cc.track_axis = 'TRACK_NEGATIVE_Z'; cc.up_axis = 'UP_Y'
sc.camera = cam

# ---------- compositor bloom (Glare, Fog Glow) ----------
# The mark renders on a TRANSPARENT film. Cycles spreads the Glare bloom into transparent
# pixels with alpha 0, so a naive transparent render loses the x's halo when composited.
# Fix: set alpha = max(geometry alpha, bloom luminance), so the halo carries its own alpha
# and the frame composites with plain opacity (no screen-blend, no black square).
sc.use_nodes = True; ct = sc.node_tree
for n in list(ct.nodes):
    ct.nodes.remove(n)
rl = ct.nodes.new("CompositorNodeRLayers")
gl = ct.nodes.new("CompositorNodeGlare"); gl.glare_type = 'FOG_GLOW'; gl.threshold = 0.6; gl.size = 7
bw = ct.nodes.new("CompositorNodeRGBToBW")
mx = ct.nodes.new("CompositorNodeMath"); mx.operation = 'MAXIMUM'; mx.use_clamp = True
sa = ct.nodes.new("CompositorNodeSetAlpha"); sa.mode = 'REPLACE_ALPHA'
co = ct.nodes.new("CompositorNodeComposite")
ct.links.new(rl.outputs["Image"], gl.inputs["Image"])
ct.links.new(gl.outputs["Image"], bw.inputs["Image"])
ct.links.new(bw.outputs["Val"], mx.inputs[0])
ct.links.new(rl.outputs["Alpha"], mx.inputs[1])
ct.links.new(gl.outputs["Image"], sa.inputs["Image"])
ct.links.new(mx.outputs["Value"], sa.inputs["Alpha"])
ct.links.new(sa.outputs["Image"], co.inputs["Image"])


def render(path):
    sc.render.filepath = path; bpy.ops.render.render(write_still=True)


# Both frames render TRANSPARENT (the alpha-from-bloom node keeps the halo). Base = dim x;
# glow = the x at full backlight, cross-faded on top with plain opacity in the splash.
sc.render.film_transparent = True
em.inputs["Strength"].default_value = 0.25
render(OUT + "cx-metal@3x.png")
em.inputs["Strength"].default_value = 9.0
render(OUT + "cx-metal-on@3x.png")
print("done ->", OUT)
