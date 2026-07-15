# Run inside Blender:
# Blender -b file.blend -P tools/pass6/blender_scene_manifest.py -- --output review/pass6/<product>/scene_manifest.json
import bpy, json, sys, math
from pathlib import Path
from mathutils import Vector

def arg(name, default=None):
    argv=sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else []
    return argv[argv.index(name)+1] if name in argv and argv.index(name)+1<len(argv) else default

def world_bbox(ob):
    if ob.type!='MESH': return None
    pts=[ob.matrix_world @ Vector(c) for c in ob.bound_box]
    return {'min':[min(p[i] for p in pts) for i in range(3)],'max':[max(p[i] for p in pts) for i in range(3)]}

out=Path(arg('--output','scene_manifest.json')); out.parent.mkdir(parents=True,exist_ok=True)
objects=[]
for ob in bpy.context.scene.objects:
    rec={'name':ob.name,'type':ob.type,'location':list(ob.location),'rotation_euler':list(ob.rotation_euler),'scale':list(ob.scale),'bbox_world_m':world_bbox(ob)}
    if ob.type=='MESH': rec.update({'vertices':len(ob.data.vertices),'edges':len(ob.data.edges),'faces':len(ob.data.polygons),'materials':[m.name if m else None for m in ob.data.materials],'modifiers':[{'name':m.name,'type':m.type,'show_render':m.show_render} for m in ob.modifiers]})
    objects.append(rec)
scene=bpy.context.scene
data={'pass':True,'blender_version':bpy.app.version_string,'blend_filepath':bpy.data.filepath,'scene':scene.name,'unit_system':scene.unit_settings.system,'unit_scale':scene.unit_settings.scale_length,'render_engine':scene.render.engine,'resolution':[scene.render.resolution_x,scene.render.resolution_y,scene.render.resolution_percentage],'objects':objects}
out.write_text(json.dumps(data,indent=2)+'\n'); print(out)
