import bpy, sys, math, json, importlib.util, os
from mathutils import Vector
ROOT="/Users/scammermike/Downloads/computexchange"
OUT=ROOT+"/review/pass6/mac_studio/iter_cand01"; os.makedirs(OUT, exist_ok=True)
BUILDER=ROOT+"/.claude/worktrees/model-refinement/render/build_scene.py"
PERF=ROOT+"/source/mac_studio/perforated_panel.py"
SOL=json.load(open(ROOT+"/camera_solutions/mac_studio/rear_real_01.json"))
mmpx=SOL["ortho"]["mm_per_px"]; W,H=SOL["image_size_px"]
spec=importlib.util.spec_from_file_location("v2",ROOT+"/source/mac_studio/build_mac_studio_v2.py")
v2=importlib.util.module_from_spec(spec); spec.loader.exec_module(v2)
info=v2.build_mac_studio_v2(BUILDER, PERF, yaw_deg=180.0)   # rear faces -Y camera
# strip lights/cameras from baseline
for o in list(bpy.data.objects):
    if o.type in ("LIGHT","CAMERA"): bpy.data.objects.remove(o, do_unlink=True)
sc=bpy.context.scene; sc.render.engine="CYCLES"
try: sc.cycles.device="GPU"
except: pass
sc.cycles.samples=40; sc.view_settings.view_transform="Standard"
RW=1200; RH=int(round(RW*H/W)); sc.render.resolution_x=RW; sc.render.resolution_y=RH
sc.render.image_settings.file_format="PNG"
ortho_scale=(W*mmpx)/1000.0
cam_d=bpy.data.cameras.new("c"); cam_d.type="ORTHO"; cam_d.ortho_scale=ortho_scale
cam=bpy.data.objects.new("c",cam_d); sc.collection.objects.link(cam); sc.camera=cam
cam.location=(0.0,-0.6,0.0475); cam.rotation_euler=(math.radians(90),0,0)
world=bpy.data.worlds.new("n"); sc.world=world; world.use_nodes=True
world.node_tree.nodes["Background"].inputs[0].default_value=(0.5,0.5,0.5,1)
R=0.2
for dx,dz in [(-1,0.6),(1,0.6),(0,1.1),(-1,-0.5),(1,-0.5)]:
    ld=bpy.data.lights.new("l","AREA"); ld.energy=40*R*R; ld.size=R*2
    lo=bpy.data.objects.new("l",ld); lo.location=(dx*R*2.2,-R*2.4,0.0475+dz*R*2.2)
    d=Vector((0,0,0.0475))-Vector(lo.location); lo.rotation_euler=d.to_track_quat('-Z','Y').to_euler(); sc.collection.objects.link(lo)
vl=sc.view_layers[0]
def rt(p): sc.render.filepath=p; bpy.ops.render.render(write_still=True)
rt(OUT+"/v2_rear_T2_clay.png")
# T2 wire overlay (topology proof) via freestyle
sc.render.use_freestyle=True; vl.use_freestyle=True
fs=vl.freestyle_settings
for ls in list(fs.linesets): fs.linesets.remove(ls)
ls=fs.linesets.new("w"); ls.select_crease=True; ls.select_border=True; ls.select_silhouette=True
rt(OUT+"/v2_rear_T2_wire.png"); sc.render.use_freestyle=False
# silhouette
world.node_tree.nodes["Background"].inputs[0].default_value=(1,1,1,1)
m=bpy.data.materials.new("sil"); m.use_nodes=True; nt=m.node_tree; nt.nodes.clear()
o=nt.nodes.new("ShaderNodeOutputMaterial"); e=nt.nodes.new("ShaderNodeEmission"); e.inputs[0].default_value=(0,0,0,1)
nt.links.new(e.outputs[0],o.inputs[0]); vl.material_override=m
rt(OUT+"/v2_rear_silhouette.png"); vl.material_override=None
json.dump(info, open(OUT+"/v2_build_info.json","w"), indent=2)
print("MAC V2 RENDER DONE through_holes=%d vent_removed=%s"%(info["perf_through_holes"],info["baseline_vent_removed"]))
