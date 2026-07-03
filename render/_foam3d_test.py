#!/usr/bin/env python3
# render/_foam3d_test.py · TECHNIQUE-CLASS SWITCH bake-off (grader line 149) · the foam displaced
# heightfield exhausted against the panel's "no true self-shadowing depth of open-cell metal foam"
# tell across 8 loops. This builds REAL 3D open-cell geometry on a test tile: a slab with a jittered
# 3D grid of spheres booleaned OUT, leaving a connected strut network with true through-strut depth.
# Renders a raking detail crop so the look + build/render cost can be judged BEFORE touching the device.
#   /Applications/Blender.app/Contents/MacOS/Blender -b -P render/_foam3d_test.py
import bpy, bmesh, math, random, time

random.seed(7)
MM = 0.001

def reset():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    try:
        sc.cycles.device = "GPU"
        prefs = bpy.context.preferences.addons["cycles"].preferences
        prefs.compute_device_type = "METAL"
        prefs.get_devices()
        for d in prefs.devices: d.use = True
    except Exception as e:
        print("gpu:", e)
    sc.cycles.samples = 200
    sc.view_settings.view_transform = "AgX"
    return sc

sc = reset()
t0 = time.time()

# ---- the foam slab (a 44 x 44 mm tile, 7 mm deep) ----
TW, TH, TD = 44*MM, 44*MM, 7*MM
bpy.ops.mesh.primitive_cube_add(size=1.0)
slab = bpy.context.active_object
slab.scale = (TW/2, TD/2, TH/2)   # x wide, y depth (into screen), z tall
bpy.ops.object.transform_apply(scale=True)

# ---- jittered 3D grid of icospheres, JOINED then VOXEL-REMESHED into a clean manifold union ----
PITCH = 2.05*MM; RAD = 1.16*MM; JIT = 0.5*MM
nx = int(TW/PITCH)+2; nz = int(TH/PITCH)+2; ny = int(TD/PITCH)+1
from mathutils import Matrix, Vector
bm = bmesh.new(); count = 0
for iy in range(ny):
    for iz in range(nz):
        for ix in range(nx):
            x = (ix - (nx-1)/2)*PITCH + random.uniform(-JIT, JIT)
            z = (iz - (nz-1)/2)*PITCH + random.uniform(-JIT, JIT)
            y = (iy - (ny-1)/2)*PITCH + random.uniform(-JIT, JIT)
            r = RAD * random.uniform(0.82, 1.18)
            bmesh.ops.create_icosphere(bm, subdivisions=2, radius=r, matrix=Matrix.Translation((x, y, z)))
            count += 1
cutter_mesh = bpy.data.meshes.new("cutter"); bm.to_mesh(cutter_mesh); bm.free()
cutter = bpy.data.objects.new("cutter", cutter_mesh); bpy.context.collection.objects.link(cutter)
# VOXEL REMESH unions the overlapping spheres into ONE watertight manifold surface (fixes the
# self-intersection that made the raw EXACT boolean choke). Voxel size sets the cell wall detail.
rm = cutter.modifiers.new("rm", "REMESH"); rm.mode = "VOXEL"; rm.voxel_size = 0.22*MM
bpy.ops.object.select_all(action="DESELECT"); cutter.select_set(True)
bpy.context.view_layer.objects.active = cutter
bpy.ops.object.modifier_apply(modifier="rm")
print(f"icospheres: {count}, remeshed cutter tris {len(cutter.data.polygons)}")

mod = slab.modifiers.new("foam", "BOOLEAN")
mod.operation = "DIFFERENCE"; mod.solver = "EXACT"; mod.object = cutter
bpy.context.view_layer.objects.active = slab
bpy.ops.object.modifier_apply(modifier="foam")
bpy.data.objects.remove(cutter, do_unlink=True)
bb = [slab.matrix_world @ __import__("mathutils").Vector(c) for c in slab.bound_box]
print(f"foam struts tris: {len(slab.data.polygons)}  verts {len(slab.data.vertices)}  build {time.time()-t0:.1f}s")

# smooth shade
for p in slab.data.polygons: p.use_smooth = True

# ---- champagne-gold foam (bright struts, near-black deep interior via long AO) · dark-stage look ----
m = bpy.data.materials.new("foam-gold"); m.use_nodes = True
nt = m.node_tree; b = nt.nodes["Principled BSDF"]
b.inputs["Metallic"].default_value = 0.85
b.inputs["Roughness"].default_value = 0.36
ao = nt.nodes.new("ShaderNodeAmbientOcclusion"); ao.inputs["Distance"].default_value = 3.0*MM; ao.samples = 10
aom = nt.nodes.new("ShaderNodeMixRGB"); aom.blend_type = "MULTIPLY"; aom.inputs["Fac"].default_value = 0.9
aom.inputs["Color1"].default_value = (0.70, 0.545, 0.22, 1)   # golden strut
nt.links.new(ao.outputs["Color"], aom.inputs["Color2"])
nt.links.new(aom.outputs["Color"], b.inputs["Base Color"])
slab.data.materials.append(m)

# ---- dark-staged raking key + soft fill (matches the hero lighting doctrine) ----
world = bpy.data.worlds.new("w"); world.use_nodes = True
world.node_tree.nodes["Background"].inputs[0].default_value = (0.01, 0.01, 0.012, 1)  # void black
sc.world = world
ld = bpy.data.lights.new("key", "AREA"); ld.energy = 30; ld.size = 0.10
lo = bpy.data.objects.new("key", ld); lo.location = (-0.08, -0.11, 0.08)
bpy.context.collection.objects.link(lo)
lo.rotation_euler = (math.radians(48), 0, math.radians(-30))   # raking across the face from upper-left
fd = bpy.data.lights.new("fill", "AREA"); fd.energy = 6; fd.size = 0.35
fo = bpy.data.objects.new("fill", fd); fo.location = (0.04, -0.14, 0.02)
bpy.context.collection.objects.link(fo); fo.rotation_euler = (math.radians(90), 0, 0)
cd = bpy.data.cameras.new("cam"); cam = bpy.data.objects.new("cam", cd)
bpy.context.collection.objects.link(cam); sc.camera = cam
cd.lens = 58; cd.clip_start = 0.004
cam.location = (0.0, -0.095, 0.0)
cam.rotation_euler = (math.radians(90), 0, 0)
cd.dof.use_dof = True; cd.dof.focus_distance = 0.095; cd.dof.aperture_fstop = 8.0

sc.render.resolution_x = 1200; sc.render.resolution_y = 1200
sc.cycles.use_denoising = True
sc.render.filepath = "/Users/scammermike/Downloads/computexchange/.claude/worktrees/model-refinement/render/measure_evidence/foam3d-tile.png"
tr = time.time()
bpy.ops.render.render(write_still=True)
print(f"render {time.time()-tr:.1f}s -> foam3d-tile.png")
