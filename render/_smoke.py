import bpy, sys
bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
sc.render.engine = "CYCLES"
# device
try:
    prefs = bpy.context.preferences.addons["cycles"].preferences
    for dt in ("METAL","OPTIX","CUDA"):
        try: prefs.compute_device_type = dt
        except TypeError: continue
        prefs.get_devices_for_type(dt)
        got=[d for d in prefs.devices if d.type==dt]
        for d in got: d.use=True
        if got: sc.cycles.device="GPU"; print("DEVICE", dt); break
    else: print("DEVICE CPU")
except Exception as e: print("DEVICE CPU (", e, ")")
sc.cycles.samples = 64
bpy.ops.mesh.primitive_cube_add(size=1)
bpy.ops.object.light_add(type='AREA', location=(2,-2,3)); bpy.context.object.data.energy=200
cam_d=bpy.data.cameras.new("c"); cam=bpy.data.objects.new("c",cam_d); sc.collection.objects.link(cam); sc.camera=cam
cam.location=(3,-3,2); cam.rotation_euler=(1.1,0,0.785)
sc.render.resolution_x=400; sc.render.resolution_y=300
sc.render.filepath="render/previews/_smoke.png"
bpy.ops.render.render(write_still=True)
print("SMOKE OK")
