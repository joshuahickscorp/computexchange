import bpy, sys, json, os, math
from mathutils import Vector
skel_path, out_dir, strut_r_mm, cand_id = sys.argv[sys.argv.index("--")+1:][:4]
strut_r_mm = float(strut_r_mm)
os.makedirs(out_dir, exist_ok=True)
sk = json.load(open(skel_path))
P = sk["patch_mm"]/1000.0; D = sk["depth_mm"]/1000.0
for o in list(bpy.data.objects): bpy.data.objects.remove(o, do_unlink=True)
# 1) build skeleton mesh (verts+edges), mm->m
me = bpy.data.meshes.new("foam-skel")
verts = [(v[0]/1000.0, v[1]/1000.0, v[2]/1000.0) for v in sk["verts_mm"]]
me.from_pydata(verts, [tuple(e) for e in sk["edges"]], [])
me.update()
obj = bpy.data.objects.new("dgx-foam-patch", me); bpy.context.collection.objects.link(obj)
# 2) edges -> CURVE with round bevel -> real 3D strut tubes (robust for disconnected Voronoi graphs)
r = strut_r_mm/1000.0
bpy.context.view_layer.objects.active = obj; obj.select_set(True)
bpy.ops.object.convert(target='CURVE')
obj.data.bevel_depth = r; obj.data.bevel_resolution = 1; obj.data.fill_mode = 'FULL'
bpy.ops.object.convert(target='MESH')
# 3) clip to patch box [0,P]x[0,P]x[0,D]
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(P/2, P/2, D/2))
box = bpy.context.active_object; box.scale=(P/2, P/2, D/2); bpy.ops.object.transform_apply(scale=True)
bpy.context.view_layer.objects.active = obj
mb = obj.modifiers.new("clip","BOOLEAN"); mb.operation="INTERSECT"; mb.object=box; mb.solver="FAST"
bpy.ops.object.modifier_apply(modifier="clip"); bpy.data.objects.remove(box, do_unlink=True)
# stats
tris = len(obj.data.polygons); vcount=len(obj.data.vertices)
# 4) render face-on silhouette (struts black on white) to measure open-area fraction
sc=bpy.context.scene; sc.render.engine="CYCLES"
try: sc.cycles.device="GPU"
except: pass
sc.cycles.samples=16; sc.view_settings.view_transform="Standard"
RES=700; sc.render.resolution_x=RES; sc.render.resolution_y=RES; sc.render.image_settings.file_format="PNG"
cam_d=bpy.data.cameras.new("c"); cam_d.type="ORTHO"; cam_d.ortho_scale=P*1.0
cam_d.clip_start=0.00002; cam_d.clip_end=1.0
cam=bpy.data.objects.new("c",cam_d); sc.collection.objects.link(cam); sc.camera=cam
cam.location=(P/2, P/2, D+0.05); cam.rotation_euler=(0,0,0)   # look down -Z at the patch face
w=bpy.data.worlds.new("w"); sc.world=w; w.use_nodes=True; w.node_tree.nodes["Background"].inputs[0].default_value=(1,1,1,1)
m=bpy.data.materials.new("blk"); m.use_nodes=True; nt=m.node_tree; nt.nodes.clear()
o=nt.nodes.new("ShaderNodeOutputMaterial"); e=nt.nodes.new("ShaderNodeEmission"); e.inputs[0].default_value=(0,0,0,1)
nt.links.new(e.outputs[0],o.inputs[0]); obj.data.materials.append(m); sc.view_layers[0].material_override=m
sc.render.filepath=out_dir+"/%s_silhouette.png"%cand_id; bpy.ops.render.render(write_still=True)
# clay macro (lit)
sc.view_layers[0].material_override=None
mm=bpy.data.materials.new("champ"); mm.use_nodes=True
mm.node_tree.nodes["Principled BSDF"].inputs[0].default_value=(0.62,0.52,0.33,1)
obj.data.materials.clear(); obj.data.materials.append(mm)
w.node_tree.nodes["Background"].inputs[0].default_value=(0.2,0.2,0.22,1)
cam.location=(P/2 - P*0.4, P/2 - P*0.4, D+P*0.5)
d=Vector((P/2,P/2,0))-cam.location; cam.rotation_euler=d.to_track_quat('-Z','Y').to_euler()
key=bpy.data.lights.new("k","AREA"); key.energy=6; ko=bpy.data.objects.new("k",key); ko.location=(P/2,P/2-P,D+P)
dd=Vector((P/2,P/2,0))-Vector(ko.location); ko.rotation_euler=dd.to_track_quat('-Z','Y').to_euler(); sc.collection.objects.link(ko)
sc.render.filepath=out_dir+"/%s_macro.png"%cand_id; bpy.ops.render.render(write_still=True)
print("FOAM PATCH BUILT %s tris=%d verts=%d strut_r=%.3fmm" % (cand_id, tris, vcount, strut_r_mm))
