import bpy, sys, math, json, importlib.util, os
from mathutils import Vector
ROOT="/Users/scammermike/Downloads/computexchange"
OUT=ROOT+"/review/pass6/mac_studio/iter_cand01"; os.makedirs(OUT, exist_ok=True)
spec=importlib.util.spec_from_file_location("perf",ROOT+"/source/mac_studio/perforated_panel.py")
perf=importlib.util.module_from_spec(spec); spec.loader.exec_module(perf)
for o in list(bpy.data.objects): bpy.data.objects.remove(o, do_unlink=True)
panel, intended = perf.build_perforated_panel(field_w_mm=173.0, field_h_mm=50.0, thickness_mm=3.0,
                                              pitch_mm=4.5, hole_d_mm=3.2)
genus, euler, V, E, F = perf.count_open_holes(panel)
import bmesh
bm=bmesh.new(); bm.from_mesh(panel.data); non_manifold=sum(1 for e in bm.edges if not e.is_manifold); bm.free()
delta={"candidate":"mac_cand01_perforation_field","group":"perforation_field",
       "intended_holes":intended,"measured_genus_holes":genus,"euler":euler,
       "verts":V,"edges":E,"faces":F,"non_manifold_edges":non_manifold,
       "physical_holes_proven": bool(genus>=300 and abs(genus-intended)<=3 and non_manifold==0),
       "replaces":"pass2 shader-only perforated_band on a 0.6mm cutter_box (SHADER, not geometry)"}
json.dump(delta, open(OUT+"/geometry_delta.json","w"), indent=2)
sc=bpy.context.scene; sc.render.engine="CYCLES"
try: sc.cycles.device="GPU"
except: pass
sc.cycles.samples=48; sc.view_settings.view_transform="Standard"
sc.render.resolution_x=1200; sc.render.resolution_y=520; sc.render.image_settings.file_format="PNG"
# panel material (neutral)
m=bpy.data.materials.new("alu"); m.use_nodes=True
m.node_tree.nodes["Principled BSDF"].inputs[0].default_value=(0.6,0.6,0.62,1)
panel.data.materials.append(m)
# ortho camera looking -Y at the panel (front-on)
cam_d=bpy.data.cameras.new("c"); cam_d.type="ORTHO"; cam_d.ortho_scale=0.19
cam=bpy.data.objects.new("c",cam_d); sc.collection.objects.link(cam); sc.camera=cam
cam.location=(0,-0.4,0.056); cam.rotation_euler=(math.radians(90),0,0)
vl=sc.view_layers[0]
def rt(p): sc.render.filepath=p; bpy.ops.render.render(write_still=True)
# --- T2 clay (front lit) ---
w=bpy.data.worlds.new("w"); sc.world=w; w.use_nodes=True; w.node_tree.nodes["Background"].inputs[0].default_value=(0.25,0.25,0.27,1)
key=bpy.data.lights.new("k","AREA"); key.energy=8; ko=bpy.data.objects.new("k",key); ko.location=(-0.1,-0.35,0.15)
d=Vector((0,0,0.056))-Vector(ko.location); ko.rotation_euler=d.to_track_quat('-Z','Y').to_euler(); sc.collection.objects.link(ko)
rt(OUT+"/T2_clay.png")
# --- BACKLIGHT PROOF: bright emissive plane BEHIND the panel; holes must transmit light ---
ko.hide_render=True
bpy.ops.mesh.primitive_plane_add(size=0.4, location=(0,0.06,0.056)); bl=bpy.context.active_object
bl.rotation_euler=(math.radians(90),0,0)
em=bpy.data.materials.new("emit"); em.use_nodes=True; nt=em.node_tree; nt.nodes.clear()
o=nt.nodes.new("ShaderNodeOutputMaterial"); e=nt.nodes.new("ShaderNodeEmission"); e.inputs[1].default_value=50.0
nt.links.new(e.outputs[0],o.inputs[0]); bl.data.materials.append(em)
w.node_tree.nodes["Background"].inputs[0].default_value=(0,0,0,1)
rt(OUT+"/T2_backlight_proof.png")
# measure luminous fraction: bright pixels within the panel silhouette prove open holes
print("PERF CANDIDATE01 DONE holes_intended=%d genus=%d nonmanifold=%d proven=%s"%(
      intended,genus,non_manifold,delta["physical_holes_proven"]))
