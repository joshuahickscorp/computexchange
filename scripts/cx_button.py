# computexchange action buttons: a DISTINCT metallic accent from the power knob.
# Run: /Applications/Blender.app/Contents/MacOS/Blender -b -P scripts/cx_button.py
# The knob is a dark, radial-TURNED anodized DISC (the online/offline hero). The
# buttons are their own language: a flat, horizontally-BRUSHED satin-steel BAR,
# brighter, viewed near-flat. Two states: armed (bright steel, the label glows on
# top + breathes in CSS) and disabled (dark steel — the "failed is darker" accent).
# The metal is a stretchable fill; the label + breathing glow are composited in CSS,
# so the text sits IN the metal at any button width.
import bpy, math, os
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
sc.cycles.samples = 384; sc.cycles.use_denoising = True
try: sc.cycles.denoiser = 'OPENIMAGEDENOISE'
except Exception: pass
sc.render.film_transparent = True
try: sc.view_settings.view_transform = 'AgX'; sc.view_settings.look = 'AgX - Medium High Contrast'
except Exception: sc.view_settings.view_transform = 'Filmic'
sc.render.image_settings.file_format = 'PNG'; sc.render.image_settings.color_mode = 'RGBA'
sc.render.resolution_x = 1024; sc.render.resolution_y = 256   # 4:1 bar; CSS stretches it

# wide flat panel (the bar)
bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 0))
panel = bpy.context.active_object; panel.scale = (3.6, 0.9, 0.13)
bpy.ops.object.transform_apply(scale=True)
bev = panel.modifiers.new("Bevel", 'BEVEL'); bev.width = 0.06; bev.segments = 6
bev.limit_method = 'ANGLE'; bev.angle_limit = math.radians(40)
bpy.ops.object.shade_smooth()

def brushed(base):
    m = bpy.data.materials.new("steel"); m.use_nodes = True
    nt = m.node_tree; b = nt.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (base, base, base * 1.05, 1)  # faint cool steel
    b.inputs["Metallic"].default_value = 1.0
    b.inputs["Roughness"].default_value = 0.30
    b.inputs["Anisotropic"].default_value = 0.0   # no anisotropic tangent -> no center pinch
    try: b.inputs["Coat Weight"].default_value = 0.3; b.inputs["Coat Roughness"].default_value = 0.1
    except Exception: pass
    # horizontal brush comes purely from a fine bump (bands along Y -> clean horizontal streaks)
    w = nt.nodes.new("ShaderNodeTexWave"); w.wave_type = 'BANDS'; w.bands_direction = 'Y'
    w.inputs["Scale"].default_value = 230; w.inputs["Distortion"].default_value = 0.4
    bump = nt.nodes.new("ShaderNodeBump"); bump.inputs["Strength"].default_value = 0.22
    nt.links.new(w.outputs["Fac"], bump.inputs["Height"]); nt.links.new(bump.outputs["Normal"], b.inputs["Normal"])
    return m

target = bpy.data.objects.new("Aim", None); target.location = (0, 0, 0); bpy.context.collection.objects.link(target)
def aim(o): c = o.constraints.new('TRACK_TO'); c.target = target; c.track_axis = 'TRACK_NEGATIVE_Z'; c.up_axis = 'UP_Y'
def area(n, loc, size, e, sx=None):
    l = bpy.data.lights.new(n, 'AREA'); l.energy = e
    if sx: l.shape = 'RECTANGLE'; l.size = sx; l.size_y = size
    else: l.size = size
    o = bpy.data.objects.new(n, l); o.location = loc; bpy.context.collection.objects.link(o); return o
for o in [area("Key", (-1.5, -2.0, 3.2), 3.0, 240),
          area("Strip", (2.6, -1.4, 2.2), 3.2, 460, sx=0.08),   # crisp highlight sweep across the brush
          area("Fill", (2.0, 1.6, 1.6), 2.2, 55)]:
    aim(o)
world = bpy.data.worlds.new("W"); world.use_nodes = True
world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.01, 0.01, 0.012, 1); sc.world = world

# camera: orthographic, near top-down with a slight front tilt so the bevel + brush read
cam_d = bpy.data.cameras.new("C"); cam_d.type = 'ORTHO'; cam_d.ortho_scale = 7.7
cam = bpy.data.objects.new("C", cam_d); cam.location = (0, -1.05, 4.2)
bpy.context.collection.objects.link(cam); aim(cam); sc.camera = cam

sc.use_nodes = True; ct = sc.node_tree
for n in list(ct.nodes): ct.nodes.remove(n)
rl = ct.nodes.new("CompositorNodeRLayers"); gl = ct.nodes.new("CompositorNodeGlare")
gl.glare_type = 'FOG_GLOW'; gl.threshold = 0.85; gl.size = 5
comp = ct.nodes.new("CompositorNodeComposite")
ct.links.new(rl.outputs["Image"], gl.inputs["Image"]); ct.links.new(gl.outputs["Image"], comp.inputs["Image"])

def render(p): sc.render.filepath = p; bpy.ops.render.render(write_still=True)
panel.data.materials.append(brushed(0.17)); render(OUT + "button-armed@3x.png")   # bright satin steel
panel.data.materials.clear(); panel.data.materials.append(brushed(0.038))          # dark (the failed-darker accent)
render(OUT + "button-off@3x.png")
print("done ->", OUT)
